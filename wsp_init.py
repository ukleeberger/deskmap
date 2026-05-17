#!/usr/bin/env python3
"""KDE Workspace Starter — assign installed apps to virtual desktops and launch them."""

import sys
import os
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
    QGridLayout, QSizePolicy, QAbstractItemView,
)
from PyQt6.QtCore import Qt, QSize, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QIcon, QFont, QColor


CONFIG_PATH = Path.home() / ".config" / "wsp-init" / "assignments.json"
APP_DIRS = [
    Path("/usr/share/applications"),
    Path.home() / ".local/share/applications",
]


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


# ── Launch thread ───────────────────────────────────────────────────────────

class LaunchThread(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, assignments: list[dict], desktops: list[dict]):
        super().__init__()
        # assignments: [{app, desktop_uuid}, …]
        # desktops: [{index, uuid, name}, …]
        self._assignments = assignments
        self._desktops = desktops

    def run(self):
        try:
            original_uuid = _get_current_uuid()
        except Exception:
            original_uuid = None

        # group by desktop uuid
        by_desktop: dict[str, list] = {}
        for a in self._assignments:
            by_desktop.setdefault(a["desktop_uuid"], []).append(a["app"])

        uuid_to_name = {d["uuid"]: d["name"] for d in self._desktops}

        try:
            for uuid, apps in by_desktop.items():
                name = uuid_to_name.get(uuid, uuid)
                self.progress.emit(f"Wechsle zu „{name}“ ...")
                _switch_desktop(uuid)
                time.sleep(0.3)
                for app in apps:
                    self.progress.emit(f"Starte „{app['name']}“ ...")
                    exec_parts = app["exec"].split()
                    subprocess.Popen(exec_parts, start_new_session=True)
                    time.sleep(0.2)

            if original_uuid:
                time.sleep(0.5)
                _switch_desktop(original_uuid)
                self.progress.emit("Zurück zum Ausgangs-Desktop.")

            self.finished.emit(True, "Alle Anwendungen wurden gestartet.")
        except Exception as e:
            self.finished.emit(False, f"Fehler: {e}")


# ── Assignment row widget ───────────────────────────────────────────────────

class AssignmentRow(QWidget):
    remove_requested = pyqtSignal(object)  # self
    changed = pyqtSignal()

    def __init__(self, app: dict, desktops: list[dict], current_uuid: str | None = None):
        super().__init__()
        self.app = app
        self._desktops = desktops

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)

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
        btn_remove.setToolTip("Entfernen")
        btn_remove.clicked.connect(lambda: self.remove_requested.emit(self))
        layout.addWidget(btn_remove)

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
        self.setWindowTitle("Workspace Starter")
        self.setMinimumSize(900, 580)

        self._all_apps: list[dict] = []
        self._desktops: list[dict] = []
        self._assignment_rows: list[AssignmentRow] = []
        self._launch_thread: LaunchThread | None = None

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
        title = QLabel("<b>Workspace Starter</b>")
        title.setFont(QFont("", 13))
        top.addWidget(title)
        top.addStretch()

        self._desktop_count_lbl = QLabel()
        top.addWidget(self._desktop_count_lbl)

        btn_refresh = QPushButton("Aktualisieren")
        btn_refresh.clicked.connect(self._load_desktops)
        top.addWidget(btn_refresh)
        root.addLayout(top)

        # ── Splitter: app list  |  assignments
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: installed apps
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Installierte Anwendungen"))

        self._search = QLineEdit()
        self._search.setPlaceholderText("Suchen ...")
        self._search.textChanged.connect(self._filter_apps)
        left_layout.addWidget(self._search)

        self._app_list = QListWidget()
        self._app_list.setIconSize(QSize(24, 24))
        self._app_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._app_list.itemDoubleClicked.connect(self._add_selected)
        left_layout.addWidget(self._app_list)

        btn_add = QPushButton("Hinzufügen →")
        btn_add.clicked.connect(self._add_selected)
        left_layout.addWidget(btn_add)

        splitter.addWidget(left)

        # Right: assignment list
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Zuordnung: Anwendung → Workspace"))

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        self._assignments_widget = QWidget()
        self._assignments_layout = QVBoxLayout(self._assignments_widget)
        self._assignments_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._assignments_layout.setSpacing(2)
        scroll_area.setWidget(self._assignments_widget)
        right_layout.addWidget(scroll_area)

        btn_clear = QPushButton("Alle entfernen")
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

        self._launch_btn = QPushButton("Alle starten")
        self._launch_btn.setFixedHeight(36)
        self._launch_btn.setStyleSheet("font-weight: bold; font-size: 13px;")
        self._launch_btn.clicked.connect(self._launch_all)
        bottom.addWidget(self._launch_btn)
        root.addLayout(bottom)

    # ── Data loading ────────────────────────────────────────────────────────

    def _load_apps(self):
        self._all_apps = _parse_desktop_files()
        self._populate_app_list(self._all_apps)
        self._set_status(f"{len(self._all_apps)} Anwendungen gefunden.")

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
        self._desktop_count_lbl.setText(f"{count} Workspace{'s' if count != 1 else ''} erkannt")
        for row in self._assignment_rows:
            row.update_desktops(self._desktops)
        if not self._desktops:
            QMessageBox.warning(
                self, "KWin nicht erreichbar",
                "Die virtuellen Desktops konnten nicht abgerufen werden.\n"
                "Läuft KDE Plasma?",
            )

    # ── Assignment management ────────────────────────────────────────────────

    def _add_selected(self):
        selected_items = self._app_list.selectedItems()
        if not selected_items:
            return
        existing_ids = {row.app["id"] for row in self._assignment_rows}
        for item in selected_items:
            app = item.data(Qt.ItemDataRole.UserRole)
            if app["id"] in existing_ids:
                continue
            self._add_row(app)
            existing_ids.add(app["id"])

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
            QMessageBox.information(self, "Keine Zuordnung", "Bitte zuerst Anwendungen zuordnen.")
            return
        if not self._desktops:
            QMessageBox.warning(self, "Keine Workspaces", "Workspaces konnten nicht ermittelt werden.")
            return

        assignments = [
            {"app": row.app, "desktop_uuid": row.selected_uuid()}
            for row in self._assignment_rows
        ]

        self._launch_btn.setEnabled(False)

        self._launch_thread = LaunchThread(assignments, self._desktops)
        self._launch_thread.progress.connect(self._set_status)
        self._launch_thread.finished.connect(self._on_launch_finished)
        self._launch_thread.start()

    def _on_launch_finished(self, success: bool, message: str):
        self._launch_btn.setEnabled(True)
        self._set_status(message)
        if not success:
            QMessageBox.critical(self, "Fehler beim Starten", message)

    # ── Config persistence ───────────────────────────────────────────────────

    def _save_config(self):
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {"app_id": row.app["id"], "desktop_uuid": row.selected_uuid()}
            for row in self._assignment_rows
        ]
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load_config(self):
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        app_by_id = {a["id"]: a for a in self._all_apps}
        desktop_uuids = {d["uuid"] for d in self._desktops}
        for entry in data:
            app = app_by_id.get(entry.get("app_id"))
            uuid = entry.get("desktop_uuid")
            if app and uuid in desktop_uuids:
                self._add_row(app, uuid)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _set_status(self, text: str):
        self._status_lbl.setText(text)


# ── Entry point ─────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("wsp-init")
    app.setOrganizationName("wsp-init")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
