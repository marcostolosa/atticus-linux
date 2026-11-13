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
  tesseract) echo "tesseract tesseract-data-eng";;
  parecord|pw-record) echo "pipewire-pulse";;
  espeak-ng) echo "espeak-ng";;
  ffmpeg) echo "ffmpeg";;
  *) echo "$1";;
esac; }
for t in grim slurp wl-copy tesseract ffmpeg; do
  command -v "$t" >/dev/null || need_sys+=("$(pkg_for "$t")")
done
python3 -c 'import gi; gi.require_version("WebKit","6.0"); gi.require_version("Gtk4LayerShell","1.0")' 2>/dev/null \
  || need_sys+=("webkitgtk-6.0 gtk4-layer-shell python-gobject")
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

cat <<EOF

Next steps (Omarchy / Hyprland uses Lua config)
-----------------------------------------------
1) Hotkey. Add to ~/.config/hypr/bindings.lua:

    o.bind("CTRL + ALT + SPACE", "Atticus (voice)", "$BIN/atticus-ctl toggle")

   Then reload + validate:
    hyprctl reload && hyprctl configerrors

2) Autostart at login. Add to ~/.config/hypr/autostart.lua:

    o.exec_on_start("$BIN/atticus")

3) Start now without logging out:   setsid -f $BIN/atticus
4) Press Ctrl+Alt+Space, talk, press again to send.

   (Toggle mode: press to listen, press again to send. Hold-to-talk with a
    release bind can be added later once a reliable Hyprland bindr path is set.)
EOF
