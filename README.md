<h1 align="center"><img src="docs/hero.svg" width="800" alt="Henchline: talk to all your AI henchmen. By voice, by phone, by text."></h1>

<p align="center"><img src="docs/panels/boss.png" width="900" alt="You're the boss. Claude Code, Cursor and Codex are your crew, and Henchline is your line to all of them."></p>

Talk to them at your desk without touching the keyboard, call them from your phone when you're out, and let them ring or text you when they need a decision. Every henchman has its own voice, its own ringtone, its own chat. And it all runs on your own machine.

<p align="center">
<img src="docs/panels/ring.png" width="900" alt="They ring you: full screen with the henchman, its ringtone and why it's calling; a banner while you use the phone">
<img src="docs/panels/talk.png" width="900" alt="Talk or text, any time: a chat per henchman, and texts you answer from the notification">
<img src="docs/panels/files.png" width="900" alt="Files both ways, your whole crew: share anything to the henchman you pick; the crew menu">
</p>

## What it does

**At your desk**

- 🎙 **Push-to-talk.** Hold the Copilot key (or any key), speak, let go: it is written down on your machine as you speak and typed into the agent you're looking at. A word you stress arrives as `*word*`; a tone that stands out arrives as `[tone: loud, fast]`, so the agent knows how you said it.
- 🔊 **They talk back.** Each agent speaks its replies aloud in its own voice (Claude, Cursor and Codex sound different), one at a time, never over each other, and goes quiet the moment you start talking.
- 🙌 **Hands-free.** Say *"hey Claude"* (your own trained wake word) or tap your earbuds. Answer right after they speak, no wake word needed. Talk over a reply to cut it off. *"Over"* sends at once, *"give me a minute"* keeps listening.
- 🔀 **Switch by voice.** *"Switch me to Cursor"*, *"hand it back to Claude"*, *"send that to Codex"*: fixed phrases, announced out loud, so a mishearing is undone with *"back to …"*.

**Away from it: the Henchline app**

- 📞 **Call your crew** over your own Tailscale network. Your computer stays quiet; your phone is its mic and speaker. Open line, hold-to-talk, or typed; your words show in the chat exactly as they were heard.
- 🔔 **They ring you.** `handsfree ring "the build finished, want me to deploy?"` rings your phone: full screen when it's locked, a ringing banner while you use it, with the henchman's own tone and phrase (hear [Claude](call/android/app/src/main/res/raw/banner_claude.wav), [Cursor](call/android/app/src/main/res/raw/banner_cursor.wav) and [Codex](call/android/app/src/main/res/raw/banner_codex.wav): the sound files open on GitHub with a play button). Answer, and you're talking to the one who rang. The agent learns whether you answered, declined, missed it, or were out of reach. Your own agents get through Do Not Disturb; nobody else does.
- 💬 **They text you.** `handsfree text "tests are green"`: a message with a reply box. Your reply is typed into that agent's terminal, and only that one's. Start a text from any agent's chat in the app, too.
- 🎤 **Voice messages.** No call needed: hold the mic in a chat and speak, slide up to lock it, slide left to drop it. Whisper on your computer writes it down, the agent gets the words, and your bubble shows what was heard.
- 🧩 **Every Cursor chat its own.** Each chat in the Cursor IDE shows on the phone under Cursor's own title, and what you send lands in that chat, not whichever one is open (with the [Henchline bridge](cursor-bridge/) extension), even with your laptop locked. Its model shows on the phone, and you can switch it and stop it from there too.
- ➕ **New chat starts an agent.** Pick a folder your agent already trusts: Claude, Codex or the Cursor CLI starts there in tmux (`tmux attach -t henchline` to watch) and its chat opens. Long-press a chat to clear it, or to end an agent the phone started.
- 📎 **Files both ways.** Share a photo, a voice memo, a zip, a video from any app, or tap the paperclip: it lands on your computer and the agent is told where. An agent runs `handsfree send report.pdf "here you go"` and it shows in its chat. On a call, the camera button snaps a photo that goes with what you say next; say "show me" and the agent runs `handsfree show`, a picture of its terminal (or `--screen`) in the chat.
- 🗂 **A chat per henchman**, kept on the phone with times and read ticks, your terminal conversation mirrored into it during a call. A call history shows who rang, why, and how long you talked.
- 🎧 **Earbuds as push-to-talk**: tap to talk, tap to send, the phone stays in your pocket. Talk/Send and Hang up on the lock screen, and a Quick Settings tile if you want one.

**For your agents**

One snippet (`agents/AGENTS-snippet.md`) teaches every agent (Claude Code, Cursor, Codex) how to speak, how your voice messages are tagged, and when to ring, text or send you something. Nothing to install inside the agents: Henchline types into their own terminal tabs and windows.

## Yours, and only yours

- **Local.** Whisper and NVIDIA's Nemotron hear you and Kokoro speaks, on your machine. No cloud speech, no relay, no account.
- **Your network.** The phone reaches your computer over your own Tailscale; only your devices can.
- **Any agent.** Not tied to one vendor: if it runs in a terminal or in Cursor, it's on the crew.
- **Your voice stays yours.** Record the henchman phrases in your own voice; they stay on your phone, never in the repository.
- **Careful by default.** Rings and replies use one-time secret addresses; secrets in what you type are masked before they reach the phone; a text goes to exactly the agent you texted.

## What is in here

Every folder has its own README with the details. Each piece is laid out the same way: its command at the top,
its modules in `core/`, its tools in `tools/`, and what gets installed into the system (units, configs) in `system/`.

| Folder | What it does | Needs |
|---|---|---|
| [`ptt/`](ptt/) | **Push-to-talk:** hold a key, speak, the words are typed into the focused window. `ptt.py`; `core/` the hotkey, the paste and the tray icon; `system/` the unit and an example config. | nothing else |
| [`talkback/`](talkback/) | **Talkback:** the agents speak their replies through a local voice, one at a time. `scripts/` the `talkback` command and its worker; `SKILL.md` what agents read. | `ptt/` only to go quiet while you talk |
| [`handsfree/`](handsfree/) | **Hands-free:** talk with the agents without a key. `handsfree.py`; `core/` listening, turns, routing, the wake words, talking over; `tools/` recording, training and benchmarks; `system/` the units and the echo canceller. | `talkback/` |
| [`call/`](call/) | **Phone call and Henchline:** the call server and page; `hooks/` the Claude Code terminal mirror; `system/` the unit; [`android/`](call/android/) the app. | `handsfree/`, Tailscale |
| [`agents/`](agents/) | What the agents are told: one snippet for Claude Code, Cursor, Codex and anything reading `AGENTS.md`. | `talkback/` |
| [`cursor-bridge/`](cursor-bridge/) | A small Cursor extension: each Cursor IDE chat becomes its own chat on the phone, and replies, voice messages and model switches reach that very chat. `./install.sh agents` installs it. | Cursor |
| [`voice/`](voice/) | What all of them share: the state files (`state.py`), agents and their tabs (`agents.py`), talkback's queue (`spool.py`), files to and from the phone (`files.py`); `desktop/` windows, keys, the clipboard and terminals (KDE or any X11 desktop; Wayland: KDE, sway, Hyprland); `speech/` Whisper, Nemotron streaming and tone marks. | nothing |
| [`tests/`](tests/) | Unit tests per piece, and a throwaway-container test of the generic X11 desktop. | |
| [`docs/`](docs/) | The README's pictures and the scripts that draw them. | |

## Desktops

KDE Plasma (X11 or Wayland), sway and Hyprland on Wayland, or any other X11 desktop; everything the tools need from the desktop goes through one small interface, so another compositor is a module away (pull requests welcome). On Wayland the clipboard comes from [`wl-clipboard`](https://github.com/bugaevc/wl-clipboard) and keys from [`ydotool`](https://github.com/ReimuNotMoe/ydotool) (or `wtype` on sway), which the installer checks for; KDE's windows and focus come from KWin's own scripting interface, sway's and Hyprland's from `swaymsg` and `hyprctl`. More: [voice/README.md](voice/README.md).

## Requirements

- Linux, X11 or Wayland
- PipeWire (`pw-record`, `pw-play`)
- KDE Klipper (`qdbus org.kde.klipper`) on X11
- Python 3.12 (the system one; the venv borrows only PyQt5 and evdev from apt)
- Your user in the `input` group
- On Wayland: `wl-clipboard`, and `ydotool` (started as `ydotoold`) or `wtype`; `grim` for `handsfree show --screen` on sway/Hyprland (KDE uses Spectacle)

While the laptop is plugged in, Whisper small stays on the GPU. On battery it runs on the CPU, so a CUDA context is not held and the GPU can sleep. The microphone is not opened until the key is down.

## Install

```bash
git clone https://github.com/NinjaTuna007/henchline
cd henchline
sudo apt install python3-venv python3-pyqt5 python3-evdev curl
./install.sh ptt          # or: talkback, handsfree, agents, all
```

Each piece installs on its own and only what it needs. Python packages go into one private venv, `~/.local/share/henchline/venv` (its path is written in each script's first line, since a shebang cannot read a setting, and in `voice/state.py`), so nothing in the system or your own Python changes, and nothing there can break these tools; `requirements/` lists them per piece (the CUDA libraries only when `nvidia-smi` exists). `./doctor.py [piece]` checks each piece and prints the command that fixes whatever is missing (it runs on the system Python, so it works when the venv is the problem). `./install.sh remove <piece>` takes a piece out again and leaves settings, voice models and wake-word recordings in place. The first start downloads the Whisper small weights. The user service starts again at graphical login.

## Models

Everything runs on your machine. These are the models Henchline uses, what each is for, and how it gets onto your computer; the installer fetches each one from its publisher, and none is part of this repository.

| Model | What it does here | Runs on | Size | License | Installed by |
|---|---|---|---|---|---|
| [Whisper small](https://huggingface.co/Systran/faster-whisper-small) (via [faster-whisper](https://github.com/SYSTRAN/faster-whisper)) | Voice messages from the phone; checks for your own words echoing back; the fallback when Nemotron or Whistle is missing | GPU on mains (NVIDIA), else CPU | ~480 MB | MIT | downloaded on first start (`./install.sh ptt`) |
| [NVIDIA Nemotron Speech Streaming](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b) (as [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)'s int8 export) | Hands-free turns and push-to-talk, written down while you talk (and the "over" at the end) | CPU | ~630 MB | [NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/) | `./install.sh handsfree` |
| [Whistle](https://cactuscompute.com/blog/whistle) ([Cactus Compute](https://github.com/cactus-compute/cactus)) | Confirms the wake word in about a tenth of a second | CPU | 17 MB | Apache-2.0 | `./install.sh handsfree` |
| [Kokoro 82M](https://huggingface.co/hexgrad/Kokoro-82M) (via [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx)) | Talkback: every agent's voice | CPU | ~350 MB | Apache-2.0 | `./install.sh talkback` |
| [openWakeWord](https://github.com/dscripka/openWakeWord), with your own "hey Claude" / "hey Jarvis" detectors trained on it | Wake word detection | CPU | ~5 MB | Apache-2.0; its ready-made "hey Jarvis" model is [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) (non-commercial) | `./install.sh handsfree`; your detectors: `handsfree/tools/wakeword_collect.py`, then `wakeword_train.py` |
| [Silero VAD](https://github.com/snakers4/silero-vad) (bundled with faster-whisper) | Is someone speaking: turn ends, talk-over | CPU | ~1 MB | MIT | with faster-whisper (pip) |

To fetch or refresh them all: `./install.sh all` (or the piece's name); `./doctor.py` says which are missing and the command that fixes it. To use Whisper for turns and push-to-talk instead of Nemotron, set `"streaming": false` in `~/.config/ptt/config.json`.

## Push-to-talk

Hold the Copilot key (or any key you choose), speak, let go: it is written down on your machine as you speak and typed into the window in front, with Enter. A stressed word arrives as `*word*`, an unusual tone as `[tone: loud, fast]`. Right-click the tray icon for language, power and the hotkey. More: [ptt/README.md](ptt/README.md).

## Talkback

<img src="docs/peek_claude.svg" align="right" width="190" alt="">

Your agents speak their replies aloud, each in its own voice, one at a time, through Kokoro on your machine. They go quiet the moment you start talking. One snippet teaches every agent when and how to speak. More: [talkback/README.md](talkback/README.md).

<br clear="right">

## Hands-free

<img src="docs/peek_handsfree.svg" align="left" width="190" alt="">

Talk to your agents without touching a key. Answer right after they speak; say *"hey Claude"* (your own trained wake word) or tap your earbuds the rest of the time. Talk over a reply to cut it off; *"over"* sends at once, *"give me a minute"* keeps listening, *"switch me to Cursor"* hands you over, announced out loud. An echo canceller keeps it from hearing itself. NVIDIA's Nemotron Speech Streaming writes your words down while you are still talking, on the CPU, so they are ready a moment after you stop, even on battery (Whisper takes over if it isn't installed). More: [handsfree/README.md](handsfree/README.md).

<br clear="left">

## Phone call

<img src="docs/peek_cursor.svg" align="right" width="190" alt="">

The same conversation from your phone, over your own Tailscale network: `./install.sh call` runs a small server only your devices can reach. Your phone becomes the mic and speaker; the computer stays quiet. Open line, hold-to-talk or typed, a chat per agent, and switching by voice as at your desk. More: [call/README.md](call/README.md).

<br clear="right">

### Henchline, the Android app

<img src="docs/peek_codex.svg" align="left" width="190" alt="">

The call as a real app (Android 10+): it keeps going with the screen off, rings full screen with each henchman's own tone, takes texts you reply to from the notification, carries files both ways, keeps a chat per agent, and turns your earbuds into push-to-talk. **[Download the APK](https://github.com/NinjaTuna007/henchline/releases/latest)**, or build it yourself with `./gradlew assembleDebug` in `call/android/` and install over USB. More: [call/android/README.md](call/android/README.md).

<br clear="left">

## License

MIT

`install.sh handsfree` downloads [NVIDIA Nemotron Speech Streaming](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b) (as [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)'s int8 export) from its publishers; it is licensed by NVIDIA Corporation under the [NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/), not MIT, and is not part of this repository.
