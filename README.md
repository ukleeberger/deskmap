# Deskmap

Assigns installed apps to KDE virtual desktops and launches them there at the press of a button.

## Features

- Assign apps to virtual desktops via drag & drop
- The same app can be assigned to multiple desktops
- Tile windows evenly when multiple apps share a workspace (toggleable)
- Profiles: save and switch between named configurations
- Headless mode: launch a saved configuration directly without the GUI
- Internationalization (i18n) support via gettext (German included)
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

Creates a symlink at `~/.local/bin/deskmap` and places `deskmap.desktop` (GUI) and `deskmap-default.desktop` (launches the `default` profile headlessly) on the desktop.

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

## Window Tiling

When the **Tile windows** checkbox is enabled (default: on), Deskmap automatically arranges windows in a grid on each workspace after launching:

- Windows are distributed evenly in a grid (columns × rows calculated from `ceil(√n)`)
- Each window gets an equal share of the available screen area
- The last row is spread across the remaining space if the grid is uneven
- Tiling is handled by a temporary KWin script that is unloaded 20 seconds after launch
- Disable tiling to let KWin or another window manager handle placement

The tiling setting is saved per profile in `config.json`.

## Configuration

Stored at `~/.config/deskmap/config.json`.
