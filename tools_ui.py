# -*- coding: utf-8 -*-
"""Small dialogs shared by several tabs: register executables, move mods
to Root/ for Root Builder."""

from __future__ import annotations

import os
from typing import Callable, List, Optional

import mobase
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from . import executables as X
from . import rootbuilder as RB
from .common import PLUGIN_DISPLAY


def mo2_ini_path(organizer: mobase.IOrganizer) -> str:
    return os.path.join(organizer.basePath(), "ModOrganizer.ini")


def game_dir(organizer: mobase.IOrganizer) -> str:
    try:
        return organizer.managedGame().gameDirectory().absolutePath()
    except Exception:
        return ""


class ExecutablesDialog(QDialog):
    """Pick which detected programs to add to MO2's executables."""

    def __init__(self, organizer: mobase.IOrganizer, tools: List[X.Tool], data_dir: str,
                 root_builder_installed: bool, parent=None) -> None:
        super().__init__(parent)
        self._organizer = organizer
        self._tools = tools
        self._data_dir = data_dir
        self.setWindowTitle(f"{PLUGIN_DISPLAY} - add programs to MO2")
        self.setMinimumSize(760, 420)
        layout = QVBoxLayout(self)
        try:
            with open(mo2_ini_path(organizer), "r", encoding="utf-8", errors="replace") as h:
                present = X.existing_titles(h.read())
        except OSError:
            present = set()
        text = ("Programs found in your installed mods. Ticked ones are added to MO2's executable "
                "list when MO2 closes - MO2 rewrites that list from memory while it runs, so a "
                "change made now would be lost. They appear the next time MO2 starts.")
        if any(t.needs_root_builder for t in tools) and not root_builder_installed:
            text += ("\n\nSome of these run from the game folder and are copied there by Root "
                     "Builder, which is not installed yet.")
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)
        self._list = QListWidget()
        for tool in tools:
            exists = tool.title.lower() in present
            suffix = "  (already listed - repointed only if its program is missing)" if exists else ""
            rb = "  [via Root Builder]" if tool.needs_root_builder else ""
            item = QListWidgetItem(f"{tool.title}{rb}{suffix}\n    {tool.binary}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if exists else Qt.CheckState.Checked)
            self._list.addItem(item)
        layout.addWidget(self._list, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        apply_btn = buttons.addButton("Add when MO2 closes", QDialogButtonBox.ButtonRole.AcceptRole)
        apply_btn.clicked.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply(self) -> None:
        chosen = [t for i, t in enumerate(self._tools)
                  if self._list.item(i).checkState() == Qt.CheckState.Checked]
        if not chosen:
            self.reject()
            return
        try:
            log = X.schedule_on_exit(chosen, mo2_ini_path(self._organizer),
                                     os.path.join(self._data_dir, "executables"))
        except Exception as exc:
            QMessageBox.warning(self, PLUGIN_DISPLAY, f"Could not schedule the change:\n{exc}")
            return
        QMessageBox.information(
            self, PLUGIN_DISPLAY,
            f"{len(chosen)} program(s) will be added when you close Mod Organizer 2:\n"
            + "\n".join(f"  - {t.title}" for t in chosen)
            + f"\n\nRe-open MO2 afterwards. A backup of ModOrganizer.ini is kept next to it "
              f"(.wjat-backup); the log is at\n{log}")
        self.accept()


def offer_executables(organizer: mobase.IOrganizer, mod_folders: List[str], data_dir: str,
                      root_builder_installed: bool, curator_tools=None, parent=None,
                      quiet_if_none: bool = False) -> None:
    tools = X.detect_tools(organizer.modsPath(), mod_folders, game_dir(organizer), curator_tools)
    if not tools:
        if not quiet_if_none:
            QMessageBox.information(parent, PLUGIN_DISPLAY, "No known programs found in these mods.")
        return
    ExecutablesDialog(organizer, tools, data_dir, root_builder_installed, parent).exec()


class MoveToRootDialog(QDialog):
    """Move an installed mod's game-folder files (or all of it) into Root/."""

    def __init__(self, organizer: mobase.IOrganizer, log: Callable[..., None],
                 offer_root_builder: Optional[Callable[[str], None]] = None,
                 root_builder_installed: Callable[[], bool] = lambda: True, parent=None) -> None:
        super().__init__(parent)
        self._organizer = organizer
        self._log = log
        self._offer_rb = offer_root_builder
        self._rb_installed = root_builder_installed
        self.setWindowTitle(f"{PLUGIN_DISPLAY} - move mods to Root/")
        self.setMinimumSize(820, 520)
        layout = QVBoxLayout(self)
        label = QLabel(
            "Engine injectors, ENB, preloaders and similar files only work in the game folder. "
            "Root Builder copies a mod's Root/ folder there when MO2 launches a program. Mods "
            "marked ⚠ have such files sitting in their Data root, where they do nothing.\n"
            "'Game-folder files' moves only those files; 'Whole mod' moves everything except a "
            "Data folder (for mods that install entirely to the game folder).")
        label.setWordWrap(True)
        layout.addWidget(label)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter mods…")
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self._list, 1)
        row = QHBoxLayout()
        self._btn_files = QPushButton("Move game-folder files to Root/")
        self._btn_all = QPushButton("Move whole mod to Root/")
        self._btn_back = QPushButton("Move back from Root/")
        for b in (self._btn_files, self._btn_all, self._btn_back):
            row.addWidget(b)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._btn_files.clicked.connect(lambda: self._run("files"))
        self._btn_all.clicked.connect(lambda: self._run("all"))
        self._btn_back.clicked.connect(lambda: self._run("back"))
        self._populate()

    def _populate(self) -> None:
        mods_path = self._organizer.modsPath()
        suspects = dict(RB.suspected_injector_mods(mods_path))
        rooted = set(RB.mods_with_root(mods_path))
        names = sorted((n for n in os.listdir(mods_path)
                        if os.path.isdir(os.path.join(mods_path, n)) and not n.endswith("_separator")),
                       key=lambda n: (n not in suspects, n.lower()))
        self._list.clear()
        for name in names:
            tag = ""
            if name in suspects:
                tag = "  ⚠ " + ", ".join(suspects[name][:4])
            if name in rooted:
                tag += "  [has Root/]"
            item = QListWidgetItem(name + tag)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self._list.addItem(item)
        self._status.setText(f"{len(suspects)} mod(s) with game-folder files in their Data root; "
                             f"{len(rooted)} already use Root/.")
        self._apply_filter()

    def _apply_filter(self) -> None:
        text = self._filter.text().strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())

    def _run(self, mode: str) -> None:
        names = [i.data(Qt.ItemDataRole.UserRole) for i in self._list.selectedItems()]
        if not names:
            self._status.setText("Select one or more mods first.")
            return
        verb = {"files": "move their game-folder files into Root/",
                "all": "move everything (except a Data folder) into Root/",
                "back": "move Root/'s contents back to the mod's top level"}[mode]
        if QMessageBox.question(self, PLUGIN_DISPLAY, f"For {len(names)} mod(s), {verb}?\n\n"
                                + "\n".join(names[:15]) + ("\n…" if len(names) > 15 else ""),
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        mods_path = self._organizer.modsPath()
        changed, errors = 0, []
        for name in names:
            folder = os.path.join(mods_path, name)
            try:
                if mode == "back":
                    moved = RB.move_from_root(folder)
                else:
                    moved = RB.move_to_root(folder, everything=(mode == "all"))
                if moved:
                    changed += 1
                    self._log(f"{name}: {'restored' if mode == 'back' else 'moved to Root/'} "
                              f"{', '.join(moved[:6])}", force=True)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        try:
            self._organizer.refresh(True)
        except Exception:
            pass
        self._populate()
        msg = f"Changed {changed} mod(s)."
        if errors:
            msg += " Errors: " + "; ".join(errors[:5])
        self._status.setText(msg)
        if mode != "back" and changed and not self._rb_installed() and self._offer_rb:
            self._offer_rb("Mods now use Root/, which needs Root Builder.")
