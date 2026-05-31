#!/usr/bin/env python3
"""Deskmap — assign installed apps to virtual desktops and launch them."""

import sys
import os
import gettext
import json
import subprocess
import time
from pathlib import Path
from configparser import ConfigParser, Error as CpError

import dbus
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QListWidgetItem, QLabel, QLineEdit,
    QComboBox, QSplitter, QFrame, QMessageBox, QScrollArea,
    QGridLayout, QSizePolicy, QAbstractItemView, QInputDialog, QMenu,
    QCheckBox,
)
from PyQt6.QtCore import Qt, QSize, QThread, pyqtSignal, QTimer, QMimeData, QEvent, QPoint
from PyQt6.QtGui import QIcon, QFont, QColor, QDrag


OLD_CONFIG_PATH = Path.home() / ".config" / "deskmap" / "assignments.json"
CONFIG_PATH     = Path.home() / ".config" / "deskmap" / "config.json"
DEFAULT_PROFILE = "default"
APP_DIRS = [
    Path("/usr/share/applications"),
    Path.home() / ".local/share/applications",
]

_LOCALE_DIR = Path(__file__).resolve().parent / "locales"

try:
    _lang = gettext.translation("deskmap", localedir=_LOCALE_DIR)
except FileNotFoundError:
    _lang = gettext.NullTranslations()

_ = _lang.gettext
ngettext = _lang.ngettext


def _migrate_if_needed() -> None:
    """Migrate old assignments.json to new multi-profile config.json (silent, one-time)."""
    if CONFIG_PATH.exists():
        return
    if not OLD_CONFIG_PATH.exists():
        return
    try:
        old_data = json.loads(OLD_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    new_data = {
        "active_profile": DEFAULT_PROFILE,
        "profiles": {DEFAULT_PROFILE: old_data},
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(new_data, indent=2), encoding="utf-8")


# ── Desktop file parsing ────────────────────────────────────────────────────

def _parse_desktop_files() -> list[dict]:
    apps = {}
    for app_dir in APP_DIRS:
        if not app_dir.exists():
            continue
        for desktop in app_dir.rglob("*.desktop"):
            cp = ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(desktop, encoding="utf-8")
            except CpError:
                continue
            if "Desktop Entry" not in cp:
                continue
            entry = cp["Desktop Entry"]
            if entry.get("Type") != "Application":
                continue
            if entry.get("NoDisplay", "false").lower() == "true":
                continue
            if entry.get("Hidden", "false").lower() == "true":
                continue
            name = entry.get("Name", "").strip()
            exec_cmd = entry.get("Exec", "").strip()
            if not name or not exec_cmd:
                continue
            # strip field codes (%u, %f, …)
            exec_clean = " ".join(
                p for p in exec_cmd.split() if not (p.startswith("%") and len(p) == 2)
            )
            icon = entry.get("Icon", "application-x-executable")
            app_id = desktop.stem
            apps[app_id] = {
                "id": app_id,
                "name": name,
                "exec": exec_clean,
                "icon": icon,
                "desktop_file": str(desktop),
            }
    return sorted(apps.values(), key=lambda a: a["name"].lower())


# ── KWin / virtual desktop helpers ─────────────────────────────────────────

def _vdm():
    bus = dbus.SessionBus()
    return bus.get_object("org.kde.KWin", "/VirtualDesktopManager")


def get_desktops() -> list[dict]:
    """Return list of {index, uuid, name} sorted by index."""
    try:
        vdm = _vdm()
        raw = vdm.Get(
            "org.kde.KWin.VirtualDesktopManager", "desktops",
            dbus_interface="org.freedesktop.DBus.Properties",
        )
        return [
            {"index": int(d[0]) + 1, "uuid": str(d[1]), "name": str(d[2])}
            for d in sorted(raw, key=lambda x: int(x[0]))
        ]
    except dbus.DBusException as e:
        print(f"KWin DBus error: {e}", file=sys.stderr)
        return []


def _get_current_uuid() -> str:
    vdm = _vdm()
    return str(vdm.Get(
        "org.kde.KWin.VirtualDesktopManager", "current",
        dbus_interface="org.freedesktop.DBus.Properties",
    ))


def _switch_desktop(uuid: str) -> None:
    vdm = _vdm()
    vdm.Set(
        "org.kde.KWin.VirtualDesktopManager", "current", uuid,
        dbus_interface="org.freedesktop.DBus.Properties",
    )


# ── KWin scripting helpers ──────────────────────────────────────────────────

_KWIN_SCRIPT_NAME = "deskmap-launcher"
_KWIN_SCRIPT_PATH = "/tmp/deskmap-kwin.js"

# KWin JS: fires on every new window, moves it to the configured desktop and
# tiles it when multiple apps share the same workspace.
# __RULES__ → {identifier: [desktop_uuid, …]}
# __COUNTS__ → {desktop_uuid: total_window_count}
_KWIN_SCRIPT_TPL = """\
(function() {
    var rules  = __RULES__;
    var counts = __COUNTS__;
    var placed          = {};
    var placedOnDesktop = {};
    workspace.windowAdded.connect(function(w) {
        var fn  = (w.desktopFileName || '').toLowerCase();
        var rc  = (w.resourceClass   || '').toString().toLowerCase();
        var key = rules.hasOwnProperty(fn) ? fn
                : rules.hasOwnProperty(rc) ? rc
                : null;
        if (!key) return;
        var uuids = rules[key];
        var idx   = placed[key] || 0;
        if (idx >= uuids.length) return;
        placed[key] = idx + 1;
        var uuid = uuids[idx];
        var all  = workspace.desktops;
        var targetDesktop = null;
        for (var i = 0; i < all.length; i++) {
            if (all[i].id === uuid) {
                targetDesktop = all[i];
                w.desktops = [all[i]];
                break;
            }
        }
        var total = counts[uuid] || 0;
        if (total <= 1 || !targetDesktop) return;
        var slotIdx = placedOnDesktop[uuid] || 0;
        placedOnDesktop[uuid] = slotIdx + 1;
        try {
            var area = workspace.clientArea(2, w.output, targetDesktop);
            var cols       = Math.ceil(Math.sqrt(total));
            var rows       = Math.ceil(total / cols);
            var row        = Math.floor(slotIdx / cols);
            var col        = slotIdx % cols;
            var lastRowN   = total - (rows - 1) * cols;
            var colsInRow  = (row === rows - 1) ? lastRowN : cols;
            var winW = Math.floor(area.width  / colsInRow);
            var winH = Math.floor(area.height / rows);
            w.frameGeometry = {
                x:      area.x + col * winW,
                y:      area.y + row * winH,
                width:  winW,
                height: winH
            };
        } catch(e) {}
    });
})();
"""


def _build_kwin_rules(assignments: list[dict]) -> dict[str, list[str]]:
    """Map every identifier we know for an app to its ordered list of target desktop UUIDs."""
    rules: dict[str, list[str]] = {}
    for a in assignments:
        app, uuid = a["app"], a["desktop_uuid"]
        app_key = app["id"].lower()
        rules.setdefault(app_key, []).append(uuid)
        exec_bin = app["exec"].split()[0].split("/")[-1].lower()
        if exec_bin and exec_bin != app_key:
            rules.setdefault(exec_bin, []).append(uuid)
    return rules


def _build_kwin_counts(assignments: list[dict]) -> dict[str, int]:
    """Count how many windows will be placed on each desktop UUID (for tiling)."""
    counts: dict[str, int] = {}
    for a in assignments:
        uuid = a["desktop_uuid"]
        counts[uuid] = counts.get(uuid, 0) + 1
    return counts


def _kwin_load_script(rules: dict, counts: dict) -> bool:
    content = (
        _KWIN_SCRIPT_TPL
        .replace("__RULES__", json.dumps(rules))
        .replace("__COUNTS__", json.dumps(counts))
    )
    try:
        Path(_KWIN_SCRIPT_PATH).write_text(content, encoding="utf-8")
        bus = dbus.SessionBus()
        iface = dbus.Interface(
            bus.get_object("org.kde.KWin", "/Scripting"),
            "org.kde.kwin.Scripting",
        )
        try:
            iface.unloadScript(_KWIN_SCRIPT_NAME)
        except dbus.DBusException:
            pass
        # Force two-argument overload via explicit dbus signature
        iface.loadScript(_KWIN_SCRIPT_PATH, _KWIN_SCRIPT_NAME,
                         signature=dbus.Signature("ss"))
        iface.start()
        return True
    except Exception as e:
        print(f"KWin script error: {e}", file=sys.stderr)
        return False


def _kwin_unload_script() -> None:
    try:
        bus = dbus.SessionBus()
        iface = dbus.Interface(
            bus.get_object("org.kde.KWin", "/Scripting"),
            "org.kde.kwin.Scripting",
        )
        iface.unloadScript(_KWIN_SCRIPT_NAME)
    except Exception:
        pass
    try:
        Path(_KWIN_SCRIPT_PATH).unlink(missing_ok=True)
    except Exception:
        pass


# ── Launch thread ───────────────────────────────────────────────────────────

class LaunchThread(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, assignments: list[dict], desktops: list[dict], *, tiling: bool = True):
        super().__init__()
        self._assignments = assignments
        self._desktops = desktops
        self._tiling = tiling

    def run(self):
        rules  = _build_kwin_rules(self._assignments)
        counts = _build_kwin_counts(self._assignments) if self._tiling else {}
        if not _kwin_load_script(rules, counts):
            self.finished.emit(False, _("Could not load KWin script."))
            return

        try:
            for a in self._assignments:
                app = a["app"]
                self.progress.emit(_("Launching '{name}' ...").format(name=app["name"]))
                subprocess.Popen(app["exec"].split(), start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(0.15)
            self.finished.emit(True, _("Launched — workspace rules active (20 s)."))
        except Exception as e:
            _kwin_unload_script()
            self.finished.emit(False, _("Error: {e}").format(e=e))


# ── Assignment row widget ───────────────────────────────────────────────────

class AssignmentRow(QWidget):
    remove_requested = pyqtSignal(object)  # self
    changed = pyqtSignal()

    def __init__(self, app: dict, desktops: list[dict], current_uuid: str | None = None):
        super().__init__()
        self.app = app
        self._desktops = desktops
        self._drag_start: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)

        self._drag_handle = QLabel("⠿")
        self._drag_handle.setFixedWidth(18)
        self._drag_handle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
        self._drag_handle.setToolTip(_("Drag to reorder"))
        self._drag_handle.setStyleSheet("color: #888; font-size: 14px;")
        layout.addWidget(self._drag_handle)

        icon_lbl = QLabel()
        icon = QIcon.fromTheme(app["icon"], QIcon.fromTheme("application-x-executable"))
        icon_lbl.setPixmap(icon.pixmap(QSize(24, 24)))
        layout.addWidget(icon_lbl)

        name_lbl = QLabel(app["name"])
        name_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(name_lbl)

        self.combo = QComboBox()
        self.combo.setMinimumWidth(160)
        for d in desktops:
            self.combo.addItem(f"{d['index']}: {d['name']}", userData=d["uuid"])
        if current_uuid:
            for i, d in enumerate(desktops):
                if d["uuid"] == current_uuid:
                    self.combo.setCurrentIndex(i)
                    break
        self.combo.currentIndexChanged.connect(self.changed)
        layout.addWidget(self.combo)

        btn_remove = QPushButton("✕")
        btn_remove.setFixedSize(28, 28)
        btn_remove.setToolTip(_("Remove"))
        btn_remove.clicked.connect(lambda: self.remove_requested.emit(self))
        layout.addWidget(btn_remove)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle_rect = self._drag_handle.geometry()
            if handle_rect.contains(event.pos()):
                self._drag_start = event.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_start = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_start is not None
                and event.buttons() & Qt.MouseButton.LeftButton
                and (event.pos() - self._drag_start).manhattanLength() >= 6):
            self._drag_start = None
            drag = QDrag(self)
            mime = QMimeData()
            mime.setText(str(id(self)))
            drag.setMimeData(mime)
            pixmap = self.grab()
            drag.setPixmap(pixmap)
            drag.setHotSpot(event.pos())
            drag.exec(Qt.DropAction.MoveAction)
        super().mouseMoveEvent(event)

    def selected_uuid(self) -> str:
        return self.combo.currentData()

    def update_desktops(self, desktops: list[dict]) -> None:
        prev_uuid = self.selected_uuid()
        self._desktops = desktops
        self.combo.clear()
        for d in desktops:
            self.combo.addItem(f"{d['index']}: {d['name']}", userData=d["uuid"])
        for i, d in enumerate(desktops):
            if d["uuid"] == prev_uuid:
                self.combo.setCurrentIndex(i)
                break


# ── Main window ─────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Deskmap")
        self.setMinimumSize(900, 580)

        self._all_apps: list[dict] = []
        self._desktops: list[dict] = []
        self._assignment_rows: list[AssignmentRow] = []
        self._launch_thread: LaunchThread | None = None
        self._profiles: dict[str, list] = {DEFAULT_PROFILE: []}
        self._active_profile: str = DEFAULT_PROFILE
        self._loading: bool = False
        self._tiling_enabled: bool = True
        self._quit_after_launch: bool = False

        self._build_ui()
        self._load_apps()
        self._load_desktops()
        self._load_config()

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        # ── Top bar
        top = QHBoxLayout()
        title = QLabel("<b>Deskmap</b>")
        title.setFont(QFont("", 13))
        top.addWidget(title)
        top.addStretch()

        top.addWidget(QLabel(_("Profile:")))

        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(130)
        self._profile_combo.addItem(DEFAULT_PROFILE)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        top.addWidget(self._profile_combo)

        btn_new_profile = QPushButton(_("New"))
        btn_new_profile.setFixedWidth(44)
        btn_new_profile.setToolTip(_("Create new profile"))
        btn_new_profile.clicked.connect(self._create_profile)
        top.addWidget(btn_new_profile)

        self._btn_manage_profile = QPushButton("…")
        self._btn_manage_profile.setFixedWidth(30)
        self._btn_manage_profile.setToolTip(_("Rename or delete profile"))
        self._btn_manage_profile.clicked.connect(self._manage_profile)
        top.addWidget(self._btn_manage_profile)

        top.addSpacing(8)

        self._desktop_count_lbl = QLabel()
        top.addWidget(self._desktop_count_lbl)

        btn_refresh = QPushButton(_("Refresh"))
        btn_refresh.clicked.connect(self._load_desktops)
        top.addWidget(btn_refresh)
        root.addLayout(top)

        # ── Splitter: app list  |  assignments
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: installed apps
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel(_("Installed Applications")))

        self._search = QLineEdit()
        self._search.setPlaceholderText(_("Search..."))
        self._search.textChanged.connect(self._filter_apps)
        left_layout.addWidget(self._search)

        self._app_list = QListWidget()
        self._app_list.setIconSize(QSize(24, 24))
        self._app_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._app_list.itemDoubleClicked.connect(self._add_selected)
        left_layout.addWidget(self._app_list)

        btn_add = QPushButton(_("Add →"))
        btn_add.clicked.connect(self._add_selected)
        left_layout.addWidget(btn_add)

        splitter.addWidget(left)

        # Right: assignment list
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel(_("Assignment: App → Workspace")))

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        self._assignments_widget = QWidget()
        self._assignments_widget.setAcceptDrops(True)
        self._assignments_widget.installEventFilter(self)
        self._assignments_layout = QVBoxLayout(self._assignments_widget)
        self._assignments_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._assignments_layout.setSpacing(2)
        scroll_area.setWidget(self._assignments_widget)
        right_layout.addWidget(scroll_area)

        btn_clear = QPushButton(_("Remove all"))
        btn_clear.clicked.connect(self._clear_assignments)
        right_layout.addWidget(btn_clear)

        splitter.addWidget(right)
        splitter.setSizes([380, 480])
        root.addWidget(splitter, stretch=1)

        # ── Status bar + launch button
        bottom = QHBoxLayout()
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: gray;")
        bottom.addWidget(self._status_lbl, stretch=1)

        self._tiling_chk = QCheckBox(_("Tile windows"))
        self._tiling_chk.setChecked(True)
        self._tiling_chk.setToolTip(
            _("Distribute windows evenly on each workspace like a tiling manager")
        )
        self._tiling_chk.toggled.connect(self._on_tiling_toggled)
        bottom.addWidget(self._tiling_chk)

        self._quit_chk = QCheckBox(_("Quit after launch"))
        self._quit_chk.setChecked(False)
        self._quit_chk.setToolTip(_("Close Deskmap after all applications have been launched"))
        self._quit_chk.toggled.connect(self._on_quit_after_launch_toggled)
        bottom.addWidget(self._quit_chk)

        self._launch_btn = QPushButton(_("Launch all"))
        self._launch_btn.setFixedHeight(36)
        self._launch_btn.setStyleSheet("font-weight: bold; font-size: 13px;")
        self._launch_btn.clicked.connect(self._launch_all)
        bottom.addWidget(self._launch_btn)
        root.addLayout(bottom)

    # ── Drag-and-drop reordering ─────────────────────────────────────────────

    def eventFilter(self, obj, event):
        if obj is self._assignments_widget:
            t = event.type()
            if t == QEvent.Type.DragEnter:
                if event.mimeData().hasText():
                    event.acceptProposedAction()
                return True
            if t == QEvent.Type.DragMove:
                event.acceptProposedAction()
                return True
            if t == QEvent.Type.Drop:
                self._handle_drop(event)
                return True
        return super().eventFilter(obj, event)

    def _handle_drop(self, event):
        try:
            source_id = int(event.mimeData().text())
        except ValueError:
            return
        from_row = next((r for r in self._assignment_rows if id(r) == source_id), None)
        if from_row is None:
            return
        from_idx = self._assignment_rows.index(from_row)

        drop_y = int(event.position().y())
        to_idx = len(self._assignment_rows)
        for i, row in enumerate(self._assignment_rows):
            if drop_y < row.y() + row.height() // 2:
                to_idx = i
                break

        self._reorder_row(from_idx, to_idx)
        event.acceptProposedAction()

    def _reorder_row(self, from_idx: int, to_idx: int):
        if from_idx == to_idx:
            return
        row = self._assignment_rows.pop(from_idx)
        self._assignments_layout.removeWidget(row)
        adjusted = to_idx - 1 if to_idx > from_idx else to_idx
        self._assignment_rows.insert(adjusted, row)
        self._assignments_layout.insertWidget(adjusted, row)
        self._save_config()

    # ── Data loading ────────────────────────────────────────────────────────

    def _load_apps(self):
        self._all_apps = _parse_desktop_files()
        self._populate_app_list(self._all_apps)
        n = len(self._all_apps)
        self._set_status(
            ngettext("{n} application found.", "{n} applications found.", n).format(n=n)
        )

    def _populate_app_list(self, apps: list[dict]):
        self._app_list.clear()
        for app in apps:
            item = QListWidgetItem(app["name"])
            item.setData(Qt.ItemDataRole.UserRole, app)
            icon = QIcon.fromTheme(app["icon"], QIcon.fromTheme("application-x-executable"))
            item.setIcon(icon)
            self._app_list.addItem(item)

    def _filter_apps(self, text: str):
        text = text.lower()
        filtered = [a for a in self._all_apps if text in a["name"].lower()]
        self._populate_app_list(filtered)

    def _load_desktops(self):
        self._desktops = get_desktops()
        count = len(self._desktops)
        self._desktop_count_lbl.setText(
            ngettext("{count} workspace detected", "{count} workspaces detected", count).format(count=count)
        )
        for row in self._assignment_rows:
            row.update_desktops(self._desktops)
        if not self._desktops:
            QMessageBox.warning(
                self, _("KWin Unreachable"),
                _("Could not retrieve virtual desktops.\nIs KDE Plasma running?"),
            )

    # ── Assignment management ────────────────────────────────────────────────

    def _add_selected(self):
        selected_items = self._app_list.selectedItems()
        if not selected_items:
            return
        for item in selected_items:
            app = item.data(Qt.ItemDataRole.UserRole)
            self._add_row(app)

    def _add_row(self, app: dict, desktop_uuid: str | None = None):
        if not self._desktops:
            return
        row = AssignmentRow(app, self._desktops, desktop_uuid)
        row.remove_requested.connect(self._remove_row)
        row.changed.connect(self._save_config)
        self._assignment_rows.append(row)
        self._assignments_layout.addWidget(row)
        self._save_config()

    def _remove_row(self, row: AssignmentRow):
        self._assignment_rows.remove(row)
        self._assignments_layout.removeWidget(row)
        row.deleteLater()
        self._save_config()

    def _clear_assignments(self):
        for row in list(self._assignment_rows):
            self._remove_row(row)

    # ── Launch ──────────────────────────────────────────────────────────────

    def _launch_all(self):
        if not self._assignment_rows:
            QMessageBox.information(self, _("No Assignment"), _("Please assign applications first."))
            return
        if not self._desktops:
            QMessageBox.warning(self, _("No Workspaces"), _("Could not determine workspaces."))
            return

        assignments = [
            {"app": row.app, "desktop_uuid": row.selected_uuid()}
            for row in self._assignment_rows
        ]

        self._launch_btn.setEnabled(False)

        self._launch_thread = LaunchThread(
            assignments, self._desktops, tiling=self._tiling_enabled
        )
        self._launch_thread.progress.connect(self._set_status)
        self._launch_thread.finished.connect(self._on_launch_finished)
        self._launch_thread.start()

    def _on_launch_finished(self, success: bool, message: str):
        self._launch_btn.setEnabled(True)
        self._set_status(message)
        if success:
            QTimer.singleShot(20_000, _kwin_unload_script)
            if self._quit_after_launch:
                QApplication.quit()
        else:
            QMessageBox.critical(self, _("Launch Error"), message)

    # ── Config persistence ───────────────────────────────────────────────────

    def _on_tiling_toggled(self, checked: bool):
        self._tiling_enabled = checked
        self._save_config()

    def _on_quit_after_launch_toggled(self, checked: bool):
        self._quit_after_launch = checked
        self._save_config()

    def _save_config(self):
        if self._loading:
            return
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._profiles[self._active_profile] = [
            {"app_id": row.app["id"], "desktop_uuid": row.selected_uuid()}
            for row in self._assignment_rows
        ]
        data = {
            "active_profile": self._active_profile,
            "tiling": self._tiling_enabled,
            "quit_after_launch": self._quit_after_launch,
            "profiles": self._profiles,
        }
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load_config(self):
        _migrate_if_needed()
        self._profiles = {DEFAULT_PROFILE: []}
        self._active_profile = DEFAULT_PROFILE

        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                self._profiles = raw.get("profiles", {DEFAULT_PROFILE: []})
                if DEFAULT_PROFILE not in self._profiles:
                    self._profiles[DEFAULT_PROFILE] = []
                saved_active = raw.get("active_profile", DEFAULT_PROFILE)
                if saved_active in self._profiles:
                    self._active_profile = saved_active
                self._tiling_enabled = bool(raw.get("tiling", True))
                self._quit_after_launch = bool(raw.get("quit_after_launch", False))
            except (json.JSONDecodeError, OSError):
                pass

        self._loading = True
        self._tiling_chk.setChecked(self._tiling_enabled)
        self._quit_chk.setChecked(self._quit_after_launch)
        self._refresh_profile_combo()
        self._apply_profile(self._profiles[self._active_profile])
        self._loading = False
        self._save_config()

    def _apply_profile(self, profile_data: list[dict]):
        """Clear the assignment panel and populate it from profile_data."""
        for row in list(self._assignment_rows):
            self._assignments_layout.removeWidget(row)
            row.deleteLater()
        self._assignment_rows.clear()

        app_by_id = {a["id"]: a for a in self._all_apps}
        desktop_uuids = {d["uuid"] for d in self._desktops}
        fallback_uuid = self._desktops[-1]["uuid"] if self._desktops else None
        remapped: list[str] = []

        for entry in profile_data:
            app = app_by_id.get(entry.get("app_id"))
            uuid = entry.get("desktop_uuid")
            if not app:
                continue
            if uuid not in desktop_uuids:
                if fallback_uuid is None:
                    continue
                remapped.append(app["name"])
                uuid = fallback_uuid
            self._add_row(app, uuid)

        if remapped:
            names = "\n".join(f"  • {n}" for n in remapped)
            QMessageBox.information(
                self,
                _("Workspace No Longer Available"),
                _("The following applications were redirected to the last available workspace:"
                  "\n\n{names}\n\nPlease review and adjust the assignments.").format(names=names),
            )

    # ── Profile management ───────────────────────────────────────────────────

    def _refresh_profile_combo(self):
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        names = [DEFAULT_PROFILE] + sorted(
            n for n in self._profiles if n != DEFAULT_PROFILE
        )
        for name in names:
            self._profile_combo.addItem(name)
        idx = self._profile_combo.findText(self._active_profile)
        if idx >= 0:
            self._profile_combo.setCurrentIndex(idx)
        self._profile_combo.blockSignals(False)

    def _on_profile_changed(self, index: int):
        name = self._profile_combo.currentText()
        if not name or name == self._active_profile:
            return
        self._profiles[self._active_profile] = [
            {"app_id": row.app["id"], "desktop_uuid": row.selected_uuid()}
            for row in self._assignment_rows
        ]
        self._active_profile = name
        self._loading = True
        self._apply_profile(self._profiles[self._active_profile])
        self._loading = False
        self._save_config()

    def _create_profile(self):
        name, ok = QInputDialog.getText(self, _("New Profile"), _("Profile name:"))
        name = name.strip()
        if not ok or not name:
            return
        if name in self._profiles:
            QMessageBox.warning(self, _("Error"), _("Profile '{name}' already exists.").format(name=name))
            return
        self._profiles[self._active_profile] = [
            {"app_id": row.app["id"], "desktop_uuid": row.selected_uuid()}
            for row in self._assignment_rows
        ]
        self._profiles[name] = []
        self._active_profile = name
        self._refresh_profile_combo()
        self._loading = True
        self._apply_profile([])
        self._loading = False
        self._save_config()

    def _manage_profile(self):
        menu = QMenu(self)
        act_rename = menu.addAction(_("Rename…"))
        act_delete = menu.addAction(_("Delete"))
        if self._active_profile == DEFAULT_PROFILE:
            act_rename.setEnabled(False)
            act_delete.setEnabled(False)
        action = menu.exec(self._btn_manage_profile.mapToGlobal(
            self._btn_manage_profile.rect().bottomLeft()
        ))
        if action == act_rename:
            name, ok = QInputDialog.getText(
                self, _("Rename Profile"), _("New name:"), text=self._active_profile
            )
            name = name.strip()
            if not ok or not name or name == self._active_profile:
                return
            if name in self._profiles:
                QMessageBox.warning(self, _("Error"), _("'{name}' already exists.").format(name=name))
                return
            self._profiles[name] = self._profiles.pop(self._active_profile)
            self._active_profile = name
            self._refresh_profile_combo()
            self._save_config()
        elif action == act_delete:
            reply = QMessageBox.question(
                self, _("Delete Profile"),
                _("Really delete profile '{name}'?").format(name=self._active_profile),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            del self._profiles[self._active_profile]
            self._active_profile = DEFAULT_PROFILE
            self._refresh_profile_combo()
            self._loading = True
            self._apply_profile(self._profiles[DEFAULT_PROFILE])
            self._loading = False
            self._save_config()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _set_status(self, text: str):
        self._status_lbl.setText(text)


# ── Headless mode ───────────────────────────────────────────────────────────

def run_headless(profile_name: str | None = None) -> int:
    _migrate_if_needed()

    if not CONFIG_PATH.exists():
        print(_("No saved configuration found: {path}").format(path=CONFIG_PATH), file=sys.stderr)
        return 1

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(_("Could not read configuration: {e}").format(e=e), file=sys.stderr)
        return 1

    profiles = raw.get("profiles", {})
    if not profiles:
        print(_("No profiles in configuration."), file=sys.stderr)
        return 1

    if profile_name is None:
        profile_name = raw.get("active_profile", DEFAULT_PROFILE)
    if profile_name not in profiles:
        available = ", ".join(sorted(profiles.keys()))
        print(
            _("Profile '{name}' not found. Available: {available}").format(
                name=profile_name, available=available
            ),
            file=sys.stderr,
        )
        return 1

    data = profiles[profile_name]
    tiling_enabled = bool(raw.get("tiling", True))
    print(_("Using profile: {name}").format(name=profile_name))

    desktops = get_desktops()
    if not desktops:
        print(_("Could not retrieve KWin desktops. Is KDE Plasma running?"), file=sys.stderr)
        return 1

    app_by_id = {a["id"]: a for a in _parse_desktop_files()}
    desktop_uuids = {d["uuid"] for d in desktops}

    assignments: list[dict] = []
    for entry in data:
        app = app_by_id.get(entry.get("app_id", ""))
        uuid = entry.get("desktop_uuid", "")
        if not app:
            print(_("Warning: App '{app_id}' not found, skipping.").format(app_id=entry.get("app_id")))
            continue
        if uuid not in desktop_uuids:
            print(_("Warning: Desktop UUID for '{name}' is no longer valid, skipping.").format(name=app["name"]))
            continue
        assignments.append({"app": app, "desktop_uuid": uuid})

    if not assignments:
        print(_("No valid assignments in configuration."), file=sys.stderr)
        return 1

    rules  = _build_kwin_rules(assignments)
    counts = _build_kwin_counts(assignments) if tiling_enabled else {}
    if not _kwin_load_script(rules, counts):
        print(_("Could not load KWin script."), file=sys.stderr)
        return 1

    try:
        for a in assignments:
            app = a["app"]
            print(_("Launching '{name}' ...").format(name=app["name"]))
            subprocess.Popen(app["exec"].split(), start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.15)
    except Exception as e:
        print(_("Launch error: {e}").format(e=e), file=sys.stderr)
        _kwin_unload_script()
        return 1

    print(_("Launched. Waiting 20 s for windows..."))
    time.sleep(20)
    _kwin_unload_script()
    print(_("Done."))
    return 0


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        prog="deskmap",
        description=_("Launch applications on predefined KDE workspaces."),
    )
    parser.add_argument(
        "--headless", "-H",
        action="store_true",
        help=_("Launch saved configuration directly without GUI."),
    )
    parser.add_argument(
        "--profile", "-p",
        default=None,
        help=_("Use profile (default: last active profile from config.json)."),
    )
    args, remaining = parser.parse_known_args()

    if args.headless:
        sys.exit(run_headless(args.profile))

    app = QApplication(sys.argv)
    app.setApplicationName("deskmap")
    app.setOrganizationName("deskmap")
    app.setWindowIcon(QIcon.fromTheme("preferences-desktop-display"))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
