"""Command policy for the model's shell: an allowlist, and a sandbox for
everything else.

The model can ask to run shell commands. A list of "dangerous" patterns to ask
about is never complete — `systemctl poweroff`, `curl … | sh` and
`cat ~/.ssh/id_ed25519` all slip past one — so the default here is the other
way round. A command the model wants to run is one of:

  allow    every part is a known helper or a harmless desktop control
           (volume, media, notifications, read-only queries). Runs as is.
  sandbox  anything else. Runs inside bubblewrap: the filesystem read-only,
           secrets (SSH/GPG keys, browser profiles, tokens, Atticus's own
           data) hidden, no network, no session bus, no display — so it can
           compute and read ordinary files but cannot change, send or control
           anything. When the `atticus` system user exists (tools/setup-atticus-user.sh)
           it also runs as that user, with the home directory hidden: a flaw
           in the box then still meets ordinary file permissions, and
           everything it did is under another user id in the logs.
  outside  the model says a command must really act on the computer by
           prefixing it with `outside`. The user sees the exact command on an
           Allow card first.
  deny     never, card or not (rm -rf style commands, sudo, power, piping a
           download into a shell).

Standard library only: the gate runs this under the system python3, not the
app's virtualenv.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess

OUTSIDE = "outside"

# Never run, even with the user's Allow: a misheard word or a planted
# instruction must not be one tap away from these.
_DENY_PROGRAMS = {"rm", "rmdir", "srm", "shred", "sudo", "su", "doas", "pkexec", "dd", "mkfs", "fdisk", "parted",
                  "wipefs", "shutdown", "reboot", "halt", "poweroff", "init", "telinit", "kexec", "chroot",
                  "nc", "ncat", "netcat", "socat", "crontab", "visudo", "passwd", "chpasswd", "userdel", "usermod"}
_DENY_PATTERNS = [
    (r"\bgit\s+push\b", "git push"),
    (r"\b(?:systemctl|loginctl)\b[^;&|]*\b(?:poweroff|reboot|halt|kexec|suspend|hibernate|terminate-user|kill-user|terminate-session)\b", "power or session control"),
    (r"\b(?:curl|wget|fetch)\b[^;&]*\|\s*(?:sudo\s+)?(?:ba|z|da|k|fi)?sh\b", "piping a download into a shell"),
    (r"\b(?:ba|z|da)?sh\s+-c\b.*\b(?:curl|wget)\b.*\|", "piping a download into a shell"),
    (r":\(\)\s*\{", "a fork bomb"),
    (r">\s*/dev/(?:sd|nvme|hd|vd|mmcblk)", "writing to a disk device"),
    (r"\bmkfs\.", "formatting a disk"),
    (r"\bhyprctl\b[^;&|]*\b(?:exit|killactive\s*$)", "ending the desktop session"),
]

# Programs that need the real session (audio server, session bus, compositor,
# network manager) and are harmless to run: {name: check(args) -> bool}.
def _any(args): return True


def _no_paths(args):
    """Text filters are allowed on a pipe, never on a file: `head ~/.ssh/key`
    would hand a secret to the model. An argument that looks like a path makes
    the whole command run in the sandbox, where that secret is not visible."""
    for a in args:
        v = a.split("=", 1)[-1] if a.startswith("--") else a
        if "/" in v or v.startswith(("~", ".")) and v not in (".",):
            return False
        if not a.startswith("-") and os.path.exists(v):
            return False
    return True


def _hyprctl(args):
    words = [a for a in args if not a.startswith("-")]
    return bool(words) and words[0] in ("clients", "monitors", "activewindow", "workspaces", "activeworkspace",
                                        "layers", "version", "devices", "binds", "cursorpos", "splash", "systeminfo")


def _nmcli(args):
    words = [a for a in args if not a.startswith("-")]
    if not words:
        return True
    if words[0] in ("radio", "r"):
        return True
    read = {"status", "show", "list", "s", "l"}
    return words[0] in ("general", "g", "device", "d", "dev", "connection", "c", "con", "networking", "n") and \
        (len(words) == 1 or words[1] in read or words[:2] in (["device", "wifi"], ["d", "wifi"], ["dev", "wifi"])
         and (len(words) == 2 or words[2] in ("list", "rescan")))


def _bluetoothctl(args):
    return bool(args) and args[0] in ("show", "devices", "info", "power", "connect", "disconnect", "paired-devices", "list")


def _systemctl(args):
    words = [a for a in args if not a.startswith("-")]
    return bool(words) and words[0] in ("status", "is-active", "is-enabled", "list-timers", "list-units", "show")


_ALLOW = {
    # desktop controls
    "wpctl": _any, "playerctl": _any, "brightnessctl": _any, "notify-send": _any, "wl-paste": _any, "wl-copy": _any,
    "nmcli": _nmcli, "bluetoothctl": _bluetoothctl, "hyprctl": _hyprctl, "systemctl": _systemctl,
    # app/file launchers and file search (the gate still asks before one runs a script or program)
    "open": _any, "findfile": _any, "xdg-open": _any, "gtk-launch": _any,
    # harmless on their own
    "date": _any, "echo": _any, "printf": _any, "true": _any, "sleep": _any, "pwd": _any, "whoami": _any,
    "hostname": _any, "uname": _any, "uptime": _any, "cal": _any,
    # filters, on a pipe only
    "head": _no_paths, "tail": _no_paths, "grep": _no_paths, "sort": _no_paths, "wc": _no_paths, "cut": _no_paths,
    "tr": _no_paths, "uniq": _no_paths, "jq": _no_paths, "cat": _no_paths, "column": _no_paths, "nl": _no_paths,
}


def _tokens(cmd: str) -> list[str]:
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _is_op(tok: str) -> bool:
    return bool(tok) and not tok.strip("();<>|&\n")


def _substitutes(cmd: str) -> bool:
    """$(…), backticks or <(…) outside single quotes: text the shell would run
    that this parser never sees as a command."""
    bare = re.sub(r"'[^']*'", "''", cmd)
    return bool(re.search(r"\$\(|`|[<>]\(|\$\{", bare))


def _parts(tokens: list[str]) -> list[list[str]] | None:
    """Split into simple commands. None when the command uses something this
    parser does not model (subshells, redirection to a file, here-docs)."""
    parts, cur, i = [], [], 0
    while i < len(tokens):
        t = tokens[i]
        if not _is_op(t):
            cur.append(t)
            i += 1
            continue
        if set(t) <= set("\n") or t in (";", "&&", "||", "|", "&", "|&"):
            if cur:
                parts.append(cur)
            cur = []
            i += 1
            continue
        if t in (">", ">>", "<", ">&", "&>", "<&", ">|"):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            fd_dup = t in (">&", "<&") and re.fullmatch(r"\d|-", target)
            if not (fd_dup or target == "/dev/null"):
                return None
            if cur and re.fullmatch(r"\d", cur[-1]):   # the "2" of 2>/dev/null
                cur.pop()
            i += 2
            continue
        return None   # ( ) << and anything else
    if cur:
        parts.append(cur)
    return parts


_WRAPPERS = ("env", "nohup", "nice", "ionice", "time", "command", "exec", "setsid", "timeout", "xargs", "stdbuf", OUTSIDE)
_SHELLS = ("sh", "bash", "zsh", "dash", "fish", "ksh")


def _denied(cmd: str, tokens: list[str], depth: int = 0) -> str:
    """The reason this command is never run, or ''. Looks at the program that
    starts each simple command (past wrappers like env/timeout/xargs), and
    into the text handed to `sh -c` / `eval`, so the rule cannot be dodged by
    nesting — while a quoted word ("how to use sudo") stays just a word."""
    for part in _parts(tokens) or [[t for t in tokens if not _is_op(t)]]:
        j = 0
        while j < len(part):
            name = os.path.basename(part[j])
            if re.fullmatch(r"\w+=.*", part[j]):
                j += 1
            elif name in _WRAPPERS:
                j += 1
                while j < len(part) and (part[j].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?|\w+=.*", part[j])):
                    j += 1
            else:
                break
        if j >= len(part):
            continue
        name = os.path.basename(part[j])
        if name in _DENY_PROGRAMS or name.startswith("mkfs."):
            return name
        payload = None
        if name in _SHELLS and "-c" in part[j + 1:]:
            k = part.index("-c", j + 1)
            payload = part[k + 1] if k + 1 < len(part) else None
        elif name == "eval":
            payload = " ".join(part[j + 1:])
        if payload and depth < 4:
            try:
                why = _denied(payload, _tokens(payload), depth + 1)
            except ValueError:
                why = "unbalanced quotes in nested shell text"
            if why:
                return why
        if name == "find" and re.search(r"\s-delete\b|-exec(?:dir)?\s+(?:\S*/)?(?:rm|shred|dd|mv)\b", " ".join(part)):
            return "find that deletes"
    for rx, why in _DENY_PATTERNS:
        if re.search(rx, cmd, re.I | re.S):
            return why
    return ""


def classify(cmd: str, helpers: tuple[str, ...] = ()) -> tuple[str, str]:
    """-> (verdict, detail). verdict: allow | sandbox | outside | deny.
    detail: the reason for deny, the inner command for outside, else ''.
    `helpers`: absolute paths of Atticus's own helper programs (atticus.py),
    which do their own confirmation for anything risky."""
    cmd = cmd.strip()
    if not cmd:
        return "allow", ""
    try:
        tokens = _tokens(cmd)
    except ValueError:
        return "deny", "the command has unbalanced quotes"
    why = _denied(cmd, tokens)
    if why:
        return "deny", f"Atticus never runs this ({why})"
    if tokens and tokens[0] == OUTSIDE:
        inner = cmd[len(OUTSIDE):].strip()
        if not inner:
            return "deny", "outside needs a command"
        return "outside", inner
    uses_helper = any(t in helpers for t in tokens)
    parts = None if _substitutes(cmd) else _parts(tokens)
    ok = parts is not None and bool(parts)
    if ok:
        for part in parts:
            prog, args = part[0], part[1:]
            if prog in helpers:
                continue
            check = _ALLOW.get(prog)   # by bare name only: /tmp/evil/echo is not echo
            if not check or not check(args):
                ok = False
                break
    if ok:
        return "allow", ""
    if uses_helper:
        # A helper inside the sandbox cannot reach the desktop and would fail in confusing ways.
        return "deny", ("run Atticus helpers on their own or chained with && only: no $(…), redirection to files, "
                        "or other programs in the same command")
    return "sandbox", ""


# --- the sandbox -------------------------------------------------------------
_SECRET_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", ".claude", ".mozilla",
                ".thunderbird", ".config/atticus", ".config/gh", ".config/gcloud", ".config/op", ".config/rclone",
                ".config/chromium", ".config/google-chrome", ".config/BraveSoftware", ".config/microsoft-edge",
                ".config/vivaldi", ".config/1Password", ".config/Bitwarden", ".config/keepassxc", ".config/Signal",
                ".config/discord", ".config/Slack", ".config/git", ".config/hub", ".config/sops", ".config/age",
                ".local/share/keyrings", ".local/share/kwalletd", ".local/share/TelegramDesktop",
                ".local/share/fish", ".var/app", "snap")
_SECRET_FILES = (".netrc", ".git-credentials", ".claude.json", ".bash_history", ".zsh_history", ".python_history",
                 ".psql_history", ".mysql_history", ".node_repl_history", ".lesshst", ".npmrc", ".pypirc",
                 ".pgpass", ".my.cnf", ".vault-token", ".env")
_SECRET_NAMES = re.compile(r"(?:^|/)(?:\.env(?:\.[\w.-]+)?|id_(?:rsa|dsa|ecdsa|ed25519)[\w.-]*|[\w.-]*\.(?:pem|key|p12|pfx|kdbx|keystore|jks|asc)|"
                           r"credentials(?:\.[\w]+)?|secrets?(?:\.[\w]+)?|[\w.-]*_history|shadow|gshadow)$", re.I)


def secret_path(path: str, home: str | None = None, extra: tuple[str, ...] = ()) -> bool:
    """True for a file the model must never read directly (the Read tool is not
    sandboxed, so the gate checks its path with this)."""
    home = home or os.path.expanduser("~")
    p = os.path.realpath(os.path.expanduser(str(path)))
    for d in _SECRET_DIRS:
        root = os.path.realpath(os.path.join(home, d))
        if p == root or p.startswith(root + os.sep):
            return True
    for f in _SECRET_FILES:
        if p == os.path.realpath(os.path.join(home, f)):
            return True
    for e in extra:
        root = os.path.realpath(e)
        if p == root or p.startswith(root + os.sep):
            return True
    if p.startswith(("/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/ssh/ssh_host", "/root/", "/proc/")) and \
            not re.fullmatch(r"/proc/(?:cpuinfo|meminfo|uptime|loadavg|version)", p):
        return True
    return bool(_SECRET_NAMES.search(p))


def available() -> bool:
    return bool(shutil.which("bwrap"))


USER = "atticus"
SCRATCH_ROOT = "/var/lib/atticus/scratch"
BWRAP = "/usr/bin/bwrap"
BOX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "atticus-box")   # sudo bwrap as USER


def isolated() -> bool:
    """True when commands can run as the `atticus` user: the user exists and
    sudo lets this user run bubblewrap as it, without a password."""
    if not os.path.isdir(SCRATCH_ROOT) or not os.access(SCRATCH_ROOT, os.W_OK):
        return False
    try:
        return subprocess.run(["sudo", "-n", "-u", USER, BWRAP, "--version"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def make_scratch(name: str, root: str = SCRATCH_ROOT) -> str:
    """A working directory the `atticus` user can write and only the two of
    us can enter. Its name is the ask's, cut down to plain characters."""
    path = os.path.join(root, re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80] or "ask")
    os.makedirs(path, mode=0o700, exist_ok=True)
    subprocess.run(["setfacl", "-m", f"u:{USER}:rwx", "-d", "-m", f"u:{USER}:rwx", path], capture_output=True, timeout=5)
    return path


def wrap(cmd: str, scratch: str, home: str | None = None, extra_hidden: tuple[str, ...] = (), as_user: bool = False) -> str:
    """The same command, run inside bubblewrap. `scratch` is the one writable
    directory (and the working directory). With `as_user` it runs as the
    `atticus` user and the whole home directory is an empty folder."""
    home = home or os.path.expanduser("~")
    if not as_user:
        os.makedirs(scratch, mode=0o700, exist_ok=True)
    args = ([BOX] if as_user else ["bwrap"]) + [ "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/run", "--tmpfs", "/var/tmp",
            "--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--new-session"]
    if as_user:
        args += ["--tmpfs", home]
    for d in _SECRET_DIRS:
        p = os.path.join(home, d)
        if os.path.isdir(p) and not as_user:
            args += ["--tmpfs", p]
    for f in _SECRET_FILES:
        p = os.path.join(home, f)
        if os.path.isfile(p) and not as_user:
            args += ["--ro-bind", "/dev/null", p]
    for p in extra_hidden:
        if as_user:   # inside the empty home already
            continue
        if os.path.isdir(p):
            args += ["--tmpfs", p]
        elif os.path.isfile(p):
            args += ["--ro-bind", "/dev/null", p]
    args += ["--bind", scratch, scratch, "--chdir", scratch, "--setenv", "TMPDIR", scratch,
             "--unsetenv", "DBUS_SESSION_BUS_ADDRESS", "--unsetenv", "WAYLAND_DISPLAY", "--unsetenv", "DISPLAY",
             "--unsetenv", "SSH_AUTH_SOCK", "--unsetenv", "ANTHROPIC_API_KEY", "--unsetenv", "ANTHROPIC_AUTH_TOKEN",
             "--", "/bin/sh", "-c", cmd]
    return shlex.join(args)
