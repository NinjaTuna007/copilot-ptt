# copilot-ptt

On Linux the Copilot key is a chord, Left Meta + Left Shift + F23. The desktop does not bind it to anything. Hold that key, or another key you choose. The microphone opens only while the key is down. Whisper runs on this machine, the text is pasted into the focused window, and Enter is pressed.

People already remap that chord with [keyd](https://github.com/rvaiya/keyd) and with [remap-copilot](https://github.com/m-bartlett/remap-copilot). A few tools open a launcher from F23. Hold-to-talk with Whisper already exists as well. This program is those two ideas together.

On some laptops the Copilot key sends the chord down and up immediately, so it cannot be held. This program needs a key that stays down while you speak. If yours does not, push-to-talk on that key will not work. Set another hotkey from the status icon.

## Wayland

Wayland is not implemented. Paste sends keys with the X11 test extension, and the focused window is read with `xprop`. Pull requests that make paste work on Wayland are welcome.

## Requirements

- Linux, X11
- PipeWire (`pw-record`, `pw-play`)
- KDE Klipper (`qdbus org.kde.klipper`)
- Python 3.11 or newer
- Your user in the `input` group

While the laptop is plugged in, Whisper small stays on the GPU. On battery it runs on the CPU, so a CUDA context is not held and the GPU can sleep. The microphone is not opened until the key is down.

## Install

```bash
git clone https://github.com/NinjaTuna007/copilot-ptt
cd copilot-ptt
sudo apt install python3-pyqt5 python3-evdev
python3 -m pip install --user faster-whisper
./install.sh
```

The first start downloads the Whisper small weights. The user service starts again at graphical login.

Hold the Copilot key, speak, release. A two-key hold stays one recording until both keys are up. Right-click the speech bubble for language, power, another hotkey, or to turn the chime off. The chime is the freedesktop sound `complete.oga` if that file is installed. A missing sound is skipped. `"chime_sound"` in the config can point at another file. The Copilot key stays on when another hotkey is set.

Log: `~/.local/state/ptt/ptt.log`

## License

MIT
