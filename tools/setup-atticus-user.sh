#!/bin/sh
# Creates the `atticus` system user that the model's shell commands run as.
# Run once, as root:   sudo sh tools/setup-atticus-user.sh
#                  or: pkexec sh tools/setup-atticus-user.sh   (a password window, no terminal)
#
# What it does, and nothing else:
#   - a system user and group `atticus`, no login shell, no password, no sudo
#   - /var/lib/atticus/scratch, owned by you: one folder per request, which
#     only you and `atticus` can enter (atticus_shell/sandbox.py makes them)
#   - /etc/sudoers.d/atticus: you may run /usr/bin/bwrap as `atticus` without
#     a password. That is the only thing you can start as that user.
# Undo:  sudo userdel atticus && sudo rm -r /var/lib/atticus /etc/sudoers.d/atticus
set -eu
[ "$(id -u)" = 0 ] || { echo "Run this with sudo."; exit 1; }
WHO="${SUDO_USER:-}"
[ -n "$WHO" ] || { [ -n "${PKEXEC_UID:-}" ] && WHO="$(id -nu "$PKEXEC_UID")"; }
[ -n "$WHO" ] && [ "$WHO" != root ] || { echo "Run it with sudo or pkexec from your own account, not from a root shell."; exit 1; }
command -v bwrap >/dev/null || { echo "bubblewrap is not installed (pacman -S bubblewrap)."; exit 1; }
command -v setfacl >/dev/null || { echo "setfacl is not installed (pacman -S acl)."; exit 1; }

id atticus >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/atticus --create-home \
    --shell /usr/bin/nologin --comment "Atticus command sandbox" atticus
chmod 0755 /var/lib/atticus
install -d -o "$WHO" -g "$(id -gn "$WHO")" -m 0755 /var/lib/atticus/scratch

tmp="$(mktemp)"
printf '%s ALL=(atticus) NOPASSWD: /usr/bin/bwrap\n' "$WHO" > "$tmp"
visudo -cf "$tmp" >/dev/null || { rm -f "$tmp"; echo "sudoers check failed; nothing was installed."; exit 1; }
install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/atticus
rm -f "$tmp"

echo "Done. Check it:  sudo -n -u atticus /usr/bin/bwrap --ro-bind / / id"
