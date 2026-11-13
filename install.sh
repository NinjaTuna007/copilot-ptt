#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user" "$HOME/.config/ptt"
ln -sfn "$root/ptt.py" "$HOME/.local/bin/ptt"
install -m 644 "$root/ptt.service" "$HOME/.config/systemd/user/ptt.service"
if [ ! -f "$HOME/.config/ptt/config.json" ]; then
  install -m 644 "$root/config.example.json" "$HOME/.config/ptt/config.json"
fi
systemctl --user daemon-reload
systemctl --user enable --now ptt.service
echo "ptt is enabled for this graphical session."
