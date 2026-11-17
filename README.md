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

The brain (`atticus.py`) thinks with a Claude model through the `claude` command;
the shell (overlay, audio, hotkey, wake word, text to speech) is written for
Wayland with GTK4, WebKitGTK and gtk4-layer-shell.

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

It also binds the keys, as lines in `~/.config/hypr/bindings.lua` (a key that
already does something on your desktop is left alone and reported):

| Keys | What |
| --- | --- |
| `Ctrl+Alt+Space` | Talk: press, speak, press again to send |
| `Ctrl+Alt+D` | Dictation |
| `Ctrl+Alt+Comma` | Settings |

Change them in Settings → General → Change Shortcut: press the new keys and
the line in `bindings.lua` is rewritten and Hyprland reloaded. If Hyprland
reports an error the old line is put back. "Hold to talk" binds the key's
press and release (`o.bind_hold`) instead of a toggle.

## Settings window

`atticus-ctl settings` opens a window over the same settings Atticus the
voice assistant reads (`~/.config/atticus/settings.json`): shortcuts, the
model, safety, privacy, the dictation dictionary and history, saved rules,
usage, the feedback form and the "About you" import (your account name, your
own contact card and the addresses of your mail program) all work from it.
Saved meetings are listed, opened, copied, exported and deleted there too.
Settings → General also picks the speech model that runs on this computer
(Tiny to Large v3 Turbo), downloads it, and deletes the ones you no longer
want.

### Clicking and typing, and what keeps it safe

Atticus uses programs by the names of their own controls, through the
accessibility bus (AT-SPI): `--hands see` lists a window's controls as
`[3] button "Send"`, `click 3` makes the program run that control's action,
`fill 4 "text"` fills that one field. Keys go to one window through Hyprland,
and text is typed with `wtype`. Nothing is sent to the keyboard or mouse at the
kernel level, so Atticus needs no access to `/dev/input` or `/dev/uinput` and
your user does not have to be in the `input` group.

- The Allow card for a click or key press is built from what Atticus read from
  the control itself (its name, its kind, the program), never from the
  model's description: text on a web page or in a document can be written by
  an attacker.
- A number only works for the look that produced it. The control is found again
  and must have the same name, or nothing is done.
- Typing into a terminal asks first, showing the exact text.
- Password fields are never filled.
- Programs only publish their controls when accessibility is on; Atticus turns
  it on when it starts. A program that was already open may need a restart, and
  Electron or Chromium apps may need `--force-renderer-accessibility`.

Raw mouse and keyboard input by screen position (`ydotool`, which needs the
`input` group) is off. Turn it on with `"rawInput": true` in `settings.json`
only if you accept that: anything that reaches that daemon can type into any
window.

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
| `rawInput` | `false` | Allow clicks, drags and scrolls by screen position through `ydotool` (needs it installed and your user in the `input` group). Off by default: controls are used by name. |
| `asrModel` | `"small"` | Whisper model. `"base"` answers about 2 s sooner on a laptop CPU but mishears more. |
| `sttRemote` | `"auto"` | Use the Home Assistant server's Whisper service for speech recognition when `haUrl`/`haToken` are set (usually a larger model on a stronger machine: faster and far better with whispers). Falls back to the local model. `"off"` keeps it local. |
| `sttEntity` | auto | Which `stt.*` entity to use on that server. |
| `asrVocabulary` | `[]` | Names the local model should expect, for example `["Dom", "Faber"]`. Your Home Assistant device names are added by themselves. |
| `asrBias` | `true` | Tell the local model those names. `false` turns it off. |
| `endSilence` | `0.9` | Seconds without a voice that end a request after the wake word. |
| `instant` | `true` | Run everyday commands locally without the AI. |
| `followUp` | `"always"` | After an answer, open the microphone again so a reply needs no wake word. `"question"`: only when Atticus ends on a question. `false`: never. |
| `followUpSeconds` | `2` | How long the microphone stays open after an answer. Nobody speaks: it closes and the box goes away. |
| `hideAfter` | `2` | Seconds the box stays on screen once a conversation is over. |
| `screen` | `"never"` | `"auto"` reads the window in front before every request (the box blinks while the picture is taken). By default the screen is only looked at when the request needs it. |
| `bargeIn` | `true` | Keep listening while Atticus speaks (through PipeWire echo cancellation) so talking over it stops the speech and is taken as the next request. |
| `bargeVad` | `0.6` | How sure the voice detector must be that someone is talking over Atticus (0 to 1). Lower it if talking over it is not noticed. |
| `model` | `"sonnet"` | Which Claude model answers: `"haiku"` (fastest), `"sonnet"`, `"opus"` or `"fable"`. Also in the model menu beside the text field. Everyday commands (time, timers, volume, lights) never wait for a model. |
| `browser` | first found | Which Chromium browser Atticus drives for websites: `"brave"`, `"chromium"`, `"google-chrome-stable"`. |
| `browserPort` | `9247` | Local port Atticus's own browser window listens on. |
| `safe` | `true` | Sandbox and Allow cards (see Safety). |
| `agents` | `true` | Let long requests move to a background agent (see Background agents). `false` keeps every request in the box. |
| `agentsHidden` | `false` | Keep agent chips off the screen (for a presentation); results arrive as notifications. |
| `draw` | `true` | Let Atticus draw guides over the desktop (see Drawing on the screen). |
| `calendarDirs` | `[]` | More calendar folders, or single `.ics` files (read-only). |
| `calendarFeeds` | `[]` | Addresses of `.ics` feeds to read, as `{"name": "...", "url": "https://..."}`. |
| `calendarDefault` | `"atticus"` | The calendar new events go to (an `id` from the calendar list). |
| `contactsDirs` | `[]` | More folders or `.vcf` files to read contacts from. |
| `myCard` | account name | The name on your own contact card. |
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

## Dictation

Say it and it is typed where the cursor is, in any app. Press `Ctrl+Alt+D`
to start and again to finish; a pause of a couple of seconds finishes it too.
The keys run `atticus-ctl dictate`.

For hold-to-dictate instead, bind the key's press and release yourself in
`~/.config/hypr/bindings.lua`:

```lua
o.bind_hold("CTRL + ALT + D", "Dictate", "atticus-ctl dictate-press", "Dictate (done)", "atticus-ctl dictate-release")
```

The words are tidied first (punctuation, names, false starts). One line is
typed; several lines are pasted, because a typed line break would press Enter
and send a half-written chat message. The clipboard is put back afterwards.

## Music

"Toca Tim Maia" plays it. With the Spotify app installed the song, album,
artist or playlist is played there; without it, the sound of the best match on
YouTube is played through `mpv` (needs `mpv` and `yt-dlp`). "Pausa", "próxima",
"para" and "o que está tocando" work on either.

## Websites

For anything on a website Atticus opens the page in a browser window of its
own and works through the page itself: it reads the text and the controls,
then clicks or fills them, without moving your mouse. That window keeps its
data in `~/.config/atticus/browser`, apart from your everyday browser, whose
tabs and sign-ins are never touched. A site that needs an account has to be
signed into once in Atticus's window. Sending, buying, deleting and other
clicks that cannot be undone ask first.

## Background agents

A request that takes long (its tools have been running for 15 seconds) moves
to the background by itself and the box is free for the next one. Each
background task shows as a chip at the top-right corner: what it is doing, a
question when it needs you, the result when it is done. Up to three work at
once; more wait their turn.

```bash
atticus-ctl agents                 # the window with every task, its steps, result and follow-up field
atticus-ctl agent "pesquise ..."   # start a background task straight away
atticus-ctl move                   # send what the box is working on to the background now
```

Clicking "N agents" in the box opens the same window.

## Calendar

"O que tenho amanhã?", "marca dentista sexta às 14h", "passa a reunião de
segunda para as 16h". Events are iCalendar files, one per event, the layout
`khal` and `vdirsyncer` use:

- `~/.config/atticus/calendar/` is Atticus's own calendar; new events go there.
- Every folder under `~/.calendars/` is a calendar, changed in place. Point
  `vdirsyncer` at it to sync with a server.
- `calendarFeeds` reads a calendar from its `.ics` address (the private
  address a web calendar gives out), read-only, refreshed every 15 minutes.

Moving or deleting an event always asks first, and touches only that day of a
repeating event.

## Contacts

Put `.vcf` files in `~/.config/atticus/contacts/` (an export from a phone or a
web address book is one file with every card). `~/.contacts/` is read too.
They are only read, never changed. "Qual o telefone da Ana?" and "manda um
e-mail pro João" look names up there.

## Drawing on the screen

"Onde eu clico para exportar?" or "me explica o teorema de Pitágoras
desenhando": Atticus looks at the screen and draws over it, with a caption
that is read aloud. A "click here" step moves on when you click the marked
place; "próximo", "volta" and "fecha" work by voice, and the caption has the
same buttons. Nothing is clicked for you.

```bash
atticus-ctl draw-next | draw-back | draw-close
```

## Meetings

`atticus-ctl meeting` opens the meeting panel. Write or paste what the call
is about (or let it read your next calendar event), then start listening. It
writes down what the others say (what the computer plays) and what you say
(the microphone) separately, and when someone asks a question it suggests an
answer from your notes. In a room with no call, choose "In person" and the
microphone hears everyone.

A line appears about 3 seconds after the person stops talking; words are not
shown while they are being said. The wake word is off during a call. With
`meetingSave` on (the default) the transcript, your notes and a recap are kept
in `~/.config/atticus/meetings/` and listed in Settings; nothing is sent
anywhere except the questions answered by the AI.

```bash
atticus-ctl meeting        # open the panel
atticus-ctl meeting-stop   # stop listening (a panel with nothing heard closes)
```

## First run

`atticus-ctl setup` opens the setup window: what is installed and what is
missing (recorder, recognizer, ydotool, grim, the AI client and its sign-in),
the feature switches, and a first try of the shortcut. It opens by itself the
first time Atticus runs with no settings file.

## When it does not understand

Look at the log first: `journalctl --user -u atticus | grep '\[voice\]'`.
`heard ... by='local'` means the speech server (Home Assistant) could not be
reached and the small model on this machine took over; the line
`speech server unreachable: <why>` says why, and the box says so once. With a
fan running by the desk, the same twelve commands at the same loudness as the
fan were heard with 41% of the words wrong by the server and 80% by the local
model (56% after the low rumble is cut, which the local path does). A voice
10 dB above the fan: 11% by the server, 13% by the local model. If the server is reached over Tailscale,
keep `tailscaled` enabled at boot.
