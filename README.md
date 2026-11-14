<p align="center">
  <img src="assets/logo.svg" width="120" alt="Atticus">
</p>

<h1 align="center">Atticus for Linux</h1>

<p align="center">
  A voice assistant overlay for Omarchy (Hyprland / Wayland).
  Press a hotkey or say a wake word, speak, and get a spoken answer.
</p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Omarchy%20%2F%20Hyprland-7c5cff">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-black">
</p>

<p align="center">
  <a href="assets/demo.mp4"><img src="assets/demo.gif" width="760" alt="Say the wake word, ask in Portuguese, and Atticus opens the right channel and asks to confirm"></a>
</p>

## What it is

Atticus puts a small always-on-top panel in the corner of your screen. You start
it with a keyboard shortcut or an optional wake word, it listens, transcribes what
you said in Brazilian Portuguese or English, runs it through a command-line
assistant, and reads the answer back in a natural voice. It can also look at your
screen and search the web when the request needs it.

This is a Linux port of the macOS app [atticus-mac](https://github.com/SaarthurR/atticus-mac).
The brain is reused as is; the shell (overlay, audio, hotkey, wake word, text to
speech) is rewritten for Wayland with GTK4, WebKitGTK and gtk4-layer-shell.

## Features

- Corner overlay that never steals focus, built on wlr-layer-shell.
- Push to talk by hotkey, plus an optional always-on wake word ("siri").
- Speech recognition tuned for Brazilian Portuguese (faster-whisper), English also supported. Fully local.
- Natural neural voice with Piper, kept loaded so answers start speaking at once.
- **Instant commands, no AI round trip** (Portuguese and English): timers, alarms,
  reminders, volume, brightness, play / pause / next, shopping and to-do lists,
  Wi-Fi and Bluetooth, lock screen, open an app or a site, time and date.
- Timers, alarms and reminders ring, notify and speak, survive a restart, and can repeat daily or on weekdays.
- Routines: "every weekday at 8, tell me the weather and the news" runs by itself and is read aloud.
- Smart home through a Home Assistant server: lights, plugs, climate, covers, locks, scenes.
- Conversation mode: when Atticus ends on a question, the microphone opens for your reply.
- Screen reading (grim, tesseract) and web search when a request calls for it.
- Native click, typing, key presses and window placement, driven by what it reads off a screenshot.
- Local file search ("find my resume").
- Everything else goes to the model, which has the same abilities as tools.
- No telemetry. Runs as a systemd user service.

## Safety

The model can run shell commands, so they go through a gate (`atticus_shell/sandbox.py`):

| Kind | What happens |
| --- | --- |
| Atticus helpers and plain desktop controls (volume, media, notifications, read-only queries) | run as written |
| Any other command | runs inside a bubblewrap sandbox: files read-only, keys / passwords / browser profiles hidden, no network, no access to the desktop session |
| A command that must really change something (`outside <command>`) | you see the exact command on an Allow card first, answerable by voice or click |
| `rm`, `sudo`, power-off, `git push`, piping a download into a shell | never run |

The model's file reader also refuses keys, tokens, browser profiles and Atticus's own data.
Clicks and key presses ask first. `"safe": false` in settings turns the sandbox and cards off.

## Requirements

- Omarchy, or another Hyprland / Wayland session (v1 targets Omarchy).
- System packages: `webkitgtk-6.0`, `gtk4-layer-shell`, `python-gobject`,
  `pipewire-pulse`, `grim`, `slurp`, `wl-clipboard`, `tesseract`,
  `tesseract-data-eng`, `tesseract-data-por`, `espeak-ng`, `ffmpeg`, `fd`,
  `ripgrep`, `ydotool`, `wtype`, `playerctl`, `bubblewrap`, `libnotify`,
  `wireplumber`, `brightnessctl`.
- Python 3.11 or newer.
- A command-line assistant for the brain. By default Atticus calls the
  [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI (`claude`),
  using your own plan. A custom OpenAI or Claude-style endpoint (Ollama, LM
  Studio, and similar) can be configured instead.

The build script checks for the system packages and prints the exact `pacman`
line for anything missing. It does not install system packages for you.

## Install

```bash
git clone https://github.com/marcostolosa/atticus-linux.git
cd atticus-linux
./build.sh
```

`build.sh` creates a virtual environment, installs the Python dependencies,
downloads the default Portuguese voice, writes the `atticus` and `atticus-ctl`
launchers into `~/.local/bin`, and installs and starts the user service.

Then add the hotkey to `~/.config/hypr/bindings.lua` and reload Hyprland:

```lua
o.bind("CTRL + ALT + SPACE", "Atticus (voice)", "/home/youruser/.local/bin/atticus-ctl toggle")
```

```bash
hyprctl reload && hyprctl configerrors
```

Native click and typing need `ydotool`, which only works once your user is in
the `input` group — `build.sh` adds you to it, but that takes effect on your
**next login**, not this session. Log out and back in once after the first
install.

## Usage

Press `Ctrl+Alt+Space`, speak, then press again to send. The answer streams into
the panel and is read aloud.

Close the panel with its `X` button, with `Esc` (click the panel first), or let
it fade on its own a few seconds after the answer.

Manage it like any service:

```bash
systemctl --user status atticus      # is it running
systemctl --user restart atticus     # after changing settings
journalctl --user -u atticus -f      # live logs
```

## Configuration

Settings live in `~/.config/atticus/settings.json`. Common keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `asrLanguage` | `"auto"` | Force a recognition language, for example `"pt"`. |
| `speak` | `true` | Read answers aloud. |
| `ttsVoice` | `"pt_BR-faber-medium"` | Piper voice name under `models/`. |
| `sounds` | `true` | Play the start and stop chimes. |
| `wake` | `false` | Enable the always-on wake word. |
| `wakeWord` | `"siri"` | The word that starts listening. |
| `asrModel` | `"small"` | Whisper model. `"base"` answers about 2 s sooner on a laptop CPU but mishears more. |
| `sttRemote` | `"auto"` | Use the Home Assistant server's Whisper service for speech recognition when `haUrl`/`haToken` are set (usually a larger model on a stronger machine: faster and far better with whispers). Falls back to the local model. `"off"` keeps it local. |
| `sttEntity` | auto | Which `stt.*` entity to use on that server. |
| `instant` | `true` | Run everyday commands locally without the AI. |
| `followUp` | `true` | Open the microphone when Atticus ends on a question. |
| `safe` | `true` | Sandbox and Allow cards (see Safety). |
| `haUrl`, `haToken` | unset | Home Assistant address and a long-lived access token, for smart-home control. |
| `micFix` | `true` | Set a sane capture volume at startup. |
| `micVolume` | `45` | Capture volume percent when `micFix` is on. |

Restart the service after editing: `systemctl --user restart atticus`.

## Things to say

```
Coloca um timer de 10 minutos para o macarrão      Me acorda amanhã às 7
Me lembra de tomar o remédio todo dia às 8         Quanto tempo falta no timer?
Aumenta o volume · Volume em 40% · Muta o som      Pausa · Próxima · Que música é essa?
Adiciona leite e ovos na lista de compras          O que tem na lista de compras?
Liga a luz da sala · Apaga a luz do quarto         Abre o Chromium · Bloqueia a tela
```

Anything else ("qual a previsão do tempo?", "o que tem na minha tela?", "abre o canal
do … no YouTube") goes to the model.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

## Wake word

With `wake` on and no trained model present, Atticus uses a speech-to-text
fallback to spot the word, which works right away but costs more CPU. For a
robust always-on trigger, train a small dedicated model on a machine with an
NVIDIA GPU and copy the resulting `.onnx` into `models/`. See
[tools/train_wakeword.md](tools/train_wakeword.md) for the full guide.

## Voice

The default voice is the Piper neural voice `pt_BR-faber-medium`, downloaded by
the build script into `models/`. To use a different Piper voice, download it into
`models/` and set `ttsVoice` to its name. If no voice is present, Atticus uses
espeak-ng so speech still works.

## How it works

The brain (`atticus.py`) decides what to do and prints tagged lines on stdout.
The shell (`atticus_shell/`) renders the overlay, captures the microphone,
transcribes, runs the brain, streams the reply into the panel and speaks it. The
overlay is a wlr-layer-shell surface so it floats above windows without taking
focus. gtk4-layer-shell is preloaded before libgtk so the layer protocol is
available.

## Credits

Based on [atticus-mac](https://github.com/SaarthurR/atticus-mac) by SaarthurR,
MIT licensed. Speech recognition by [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
voice by [Piper](https://github.com/OHF-Voice/piper1-gpl).

## License

MIT. See [LICENSE](LICENSE).
