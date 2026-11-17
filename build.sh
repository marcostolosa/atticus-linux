#!/bin/bash
# Atticus for Linux (Omarchy / Hyprland) — build & install.
# Creates a venv, installs Python deps, writes launchers into ~/.local/bin,
# and prints the Hyprland keybind + autostart snippet. Re-run after changes.
set -e
cd "$(dirname "$0")"
SRC="$(pwd)"
VERSION="$(tr -d '\n' < VERSION)"

# --- platform guard --------------------------------------------------------
[ "$XDG_SESSION_TYPE" = "wayland" ] || echo "Warning: not a Wayland session; v1 targets Hyprland/Wayland."
command -v hyprctl >/dev/null || echo "Warning: hyprctl not found; v1 targets Hyprland."

# --- system dependencies (reported, not auto-installed) --------------------
need_sys=()
pkg_for() { case "$1" in
  grim|slurp|wl-copy|wl-paste) echo "grim slurp wl-clipboard";;
  tesseract) echo "tesseract tesseract-data-eng tesseract-data-por";;
  parecord|pw-record) echo "pipewire-pulse";;
  espeak-ng) echo "espeak-ng";;
  ffmpeg) echo "ffmpeg";;
  fd) echo "fd";;
  rg) echo "ripgrep";;
  playerctl) echo "playerctl";;
  bwrap) echo "bubblewrap";;
  wtype) echo "wtype";;
  notify-send) echo "libnotify";;
  wpctl) echo "wireplumber";;
  brightnessctl) echo "brightnessctl";;
  gio) echo "glib2";;
  *) echo "$1";;
esac; }
# fd/rg back the file-search tool; wtype types text; playerctl backs
# media control — all optional in the sense that Atticus still runs and
# talks without them, just with those specific abilities missing.
# bwrap is the sandbox every unlisted shell command runs in; without it each
# of those asks for an Allow card instead. wtype types text with the real
# keyboard layout; notify-send/wpctl/brightnessctl back alarms and controls.
# mpv and yt-dlp play music by name when Spotify is not installed; gio moves
# files to the trash.
for t in grim slurp wl-copy tesseract ffmpeg fd rg playerctl bwrap wtype notify-send wpctl brightnessctl mpv yt-dlp gio; do
  command -v "$t" >/dev/null || need_sys+=("$(pkg_for "$t")")
done
command -v brave >/dev/null || command -v chromium >/dev/null || command -v google-chrome-stable >/dev/null \
  || echo "Note: no Chromium browser found (brave, chromium, chrome); websites will be used by picture and mouse only."
python3 -c 'import gi; gi.require_version("WebKit","6.0"); gi.require_version("Gtk4LayerShell","1.0")' 2>/dev/null \
  || need_sys+=("webkitgtk-6.0 gtk4-layer-shell python-gobject")
# clicking programs by the names of their controls goes through the accessibility bus
python3 -c 'import gi; gi.require_version("Atspi","2.0")' 2>/dev/null || need_sys+=("at-spi2-core")
if [ ${#need_sys[@]} -gt 0 ]; then
  echo "Missing system packages. On Omarchy/Arch run:"
  echo "    sudo pacman -S --needed $(printf '%s ' "${need_sys[@]}" | tr ' ' '\n' | sort -u | tr '\n' ' ')"
  echo
fi

# --- python venv -----------------------------------------------------------
VENV="$SRC/.venv"
if [ ! -d "$VENV" ]; then
  echo "Creating venv (with system GTK/WebKit bindings)…"
  python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --only-binary=:all: faster-whisper numpy
# Wake-word runtime (a trained models/<word>.onnx is still needed to use it; without
# one, or if this cannot install, the speech-to-text fallback spots the word instead).
"$VENV/bin/pip" install -q --only-binary=:all: onnxruntime livekit-wakeword 2>/dev/null \
  || echo "Note: livekit-wakeword not installed; the wake word will use the slower speech-to-text fallback."
# Neural text-to-speech (natural voice). If it or the voice cannot be fetched,
# the app falls back to espeak-ng, so this is best-effort and never fatal.
"$VENV/bin/pip" install -q piper-tts 2>/dev/null || echo "Note: piper-tts not installed; espeak-ng voice will be used."

# Download the default pt-BR neural voice once, into ./models (git-ignored).
if "$VENV/bin/python" -c "import piper" 2>/dev/null; then
  mkdir -p "$SRC/models"
  if [ ! -f "$SRC/models/pt_BR-faber-medium.onnx" ]; then
    echo "Downloading neural voice pt_BR-faber-medium (about 63 MB, once)…"
    "$VENV/bin/python" -m piper.download_voices pt_BR-faber-medium --data-dir "$SRC/models" 2>/dev/null \
      && echo "Voice ready: pt_BR-faber-medium" \
      || echo "Note: voice download skipped (offline?); espeak-ng will be used."
  fi
fi

# The speech model that runs on this computer whenever no speech server answers.
# Fetched now (about 480 MB, once) so the first request does not wait for it.
if "$VENV/bin/python" -c "from faster_whisper.utils import download_model; download_model('small', local_files_only=True)" 2>/dev/null; then :; else
  echo "Downloading the speech model 'small' (about 480 MB, once)…"
  "$VENV/bin/python" -c "from faster_whisper.utils import download_model; download_model('small')" >/dev/null 2>&1 \
    && echo "Speech model ready." \
    || echo "Note: speech model not downloaded (offline?); it will be fetched the first time it is needed."
fi

# --- launchers -------------------------------------------------------------
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
cat > "$BIN/atticus" <<EOF
#!/bin/bash
export ATTICUS_RESOURCES_DIR="$SRC"
export PYTHONPATH="$SRC\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$VENV/bin/python" -m atticus_shell "\$@"
EOF
cat > "$BIN/atticus-ctl" <<EOF
#!/bin/bash
# Sends one command (press/release/toggle/cancel) to the running Atticus.
export PYTHONPATH="$SRC\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$VENV/bin/python" -m atticus_shell.cli control "\$@"
EOF
chmod +x "$BIN/atticus" "$BIN/atticus-ctl"
echo "Installed launchers: $BIN/atticus, $BIN/atticus-ctl (v$VERSION)"

case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: add $BIN to PATH.";; esac

# --- clicking and typing ----------------------------------------------------
# Programs are used by the names of their own controls through the
# accessibility bus (AT-SPI), and keys go to one window through Hyprland, so
# nothing here needs the `input` group or /dev/uinput. Atticus switches
# accessibility on itself when it starts. Raw mouse input by position
# (ydotool) is an opt-in: set "rawInput": true and install ydotool yourself.
# Optional hardening, run once with sudo: tools/setup-atticus-user.sh makes the
# model's own shell commands run as a separate `atticus` system user.

# --- systemd user service --------------------------------------------------
# A user service (never system/root): it needs the Wayland session, mic and
# display. On Omarchy (uwsm) the graphical session env reaches it automatically.
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s|%h/.local/bin/atticus|$BIN/atticus|" "$SRC/systemd/atticus.service" > "$UNIT_DIR/atticus.service"
if command -v systemctl >/dev/null; then
  systemctl --user daemon-reload 2>/dev/null || true
  # Make sure the session env is visible to the user manager (no-op under uwsm).
  systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR HYPRLAND_INSTANCE_SIGNATURE DISPLAY 2>/dev/null || true
  systemctl --user enable --now atticus.service 2>/dev/null \
    && echo "Service enabled and started: systemctl --user status atticus" \
    || echo "Service installed. Start it with: systemctl --user enable --now atticus"
fi

cat <<EOF

Done. Manage it like any service:
    systemctl --user status atticus     # is it running?
    systemctl --user restart atticus    # after changing settings
    journalctl --user -u atticus -f     # live logs

Shortcuts (lines in ~/.config/hypr/bindings.lua; change them in Settings):
EOF
(cd "$SRC" && PATH="$BIN:$PATH" "$VENV/bin/python" -m atticus_shell.shortcuts | sed 's/^/    /')
cat <<EOF

Press the talk keys (or say the wake word, if enabled), talk, and press again
to send. Settings: atticus-ctl settings, or ~/.config/atticus/settings.json.
EOF
