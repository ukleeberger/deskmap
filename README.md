# Deskmap

Assigns installed apps to KDE virtual desktops and launches them there at the press of a button.

## Features

- Assign apps to virtual desktops via drag & drop
- The same app can be assigned to multiple desktops
- Profiles: save and switch between named configurations
- Headless mode: launch a saved configuration directly without the GUI
- Uses KWin scripting via DBus — no patches, no KWin plugins required

## Requirements

- KDE Plasma with KWin
- Python 3.11+
- `python-pyqt6`
- `python-dbus`

On Arch/CachyOS:

```
sudo pacman -S python-pyqt6 python-dbus
```

## Installation

```
./install.sh
```

Creates a symlink at `~/.local/bin/deskmap` and places `deskmap.desktop` on the desktop.

## Usage

**Start GUI:**
```
deskmap
```

**Headless (e.g. at login):**
```
deskmap --headless
deskmap --headless --profile work
```

## Configuration

Stored at `~/.config/deskmap/config.json`.
