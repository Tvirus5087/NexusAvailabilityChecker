# -*- coding: utf-8 -*-
"""Collection import tab (step 1): load a Nexus collection, pick optional
mods, check availability, fetch what is missing."""

from __future__ import annotations

import json
import os
import shutil
from typing import Any, Callable, Dict, List, Optional, Set

import mobase
from PyQt6.QtCore import QProcess, QUrl, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import collection as C
from . import model
from .common import PLUGIN_DISPLAY, PLUGIN_VERSION, human_size
from .model import KIND_DIRECT, KIND_NEXUS, ST_DL_FAILED, ST_DOWNLOADED, ST_OK, ST_PENDING, Entry
from . import builder as B
from . import installer as I
from .tools_ui import MoveToRootDialog, offer_executables
from .workers import AvailabilityChecker, BuildThread, DownloadRunner, HttpFetcher

APP_NAME = "WabbajackAuthorToolkit"
MAX_QUERY_REPAIRS = 8


# ---------------------------------------------------------------------------
# API key storage: never in plugin settings (ModOrganizer.ini ships with
# portable Wabbajack lists), only in the user's local app data, opt-in.
# ---------------------------------------------------------------------------


def _key_file() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "nexus_api_key.txt")


def load_saved_key() -> str:
    try:
        with open(_key_file(), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def save_key(key: str) -> None:
    path = _key_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(key.strip())


def forget_key() -> None:
    try:
        os.remove(_key_file())
    except OSError:
        pass


def find_7zip() -> str:
    for candidate in (shutil.which("7z"), shutil.which("7z.exe"),
                      os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "7-Zip", "7z.exe"),
                      os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "7-Zip", "7z.exe")):
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


class CollectionTab(QWidget):
    COLUMNS = ["Install", "Mod", "File", "Version", "Category", "Source", "Local", "Status", "Notes"]

    def __init__(self, organizer: mobase.IOrganizer, data_dir: Callable[[], str],
                 log: Callable[..., None], ensure_hooks: Callable[[], None],
                 colours: Dict[str, Any], parent: Optional[QWidget] = None,
                 root_builder_installed: Callable[[], bool] = lambda: True,
                 offer_root_builder: Callable[[str], None] = lambda reason: None) -> None:
        super().__init__(parent)
        self._rb_installed = root_builder_installed
        self._offer_rb = offer_root_builder
        self._organizer = organizer
        self._data_dir = data_dir
        self._log = log
        self._ensure_hooks = ensure_hooks
        self._colours = colours
        self._http = HttpFetcher(self)
        self._api: Optional[C.Collection] = None
        self._json: Optional[C.Collection] = None
        self._coll: Optional[C.Collection] = None
        self._chosen: Set[str] = set()
        self._selected: Set[str] = set()
        self._pulled: Dict[str, List[str]] = {}
        self._rows: List[str] = []                 # row -> mod key
        self._row_of: Dict[str, int] = {}
        self._availability: Dict[str, str] = {}    # mod key -> text
        self._entries: List[Entry] = []
        self._entry_keys: List[str] = []
        self._checker: Optional[AvailabilityChecker] = None
        self.runner: Optional[DownloadRunner] = None
        self._opened: Set[str] = set()
        self._fields: List[Any] = []
        self._repairs: List[str] = []
        self._populating = False
        self._bundle_root = ""
        self._root_override: Set[str] = set()
        self._build_thread: Optional[BuildThread] = None
        self._build_plan: Optional[B.BuildPlan] = None
        self._build_ui()

    # -- UI -----------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Load a Nexus collection to prepare it for MO2 (and a later Wabbajack build): choose "
            "optional mods, check every file is still served, and fetch what is missing. The "
            "curator's rules, FOMOD choices and bundled files come from the collection's "
            "collection.json - use 'Get collection.json' or load one from disk.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Collection URL or slug:"))
        self._edit_url = QLineEdit()
        self._edit_url.setPlaceholderText("https://www.nexusmods.com/games/skyrimspecialedition/collections/qdurkx")
        row.addWidget(self._edit_url, 1)
        row.addWidget(QLabel("Revision:"))
        self._spin_rev = QSpinBox()
        self._spin_rev.setRange(0, 100000)
        self._spin_rev.setSpecialValueText("latest")
        row.addWidget(self._spin_rev)
        self._btn_load = QPushButton("Load from Nexus")
        row.addWidget(self._btn_load)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Nexus API key:"))
        self._edit_key = QLineEdit()
        self._edit_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit_key.setPlaceholderText("Personal API key from your Nexus account settings "
                                          "(needed for collection.json and adult collections)")
        saved = load_saved_key()
        self._edit_key.setText(saved)
        row.addWidget(self._edit_key, 1)
        self._chk_remember = QCheckBox("Remember on this PC")
        self._chk_remember.setChecked(bool(saved))
        self._chk_remember.setToolTip("Stored in %LOCALAPPDATA%\\WabbajackAuthorToolkit, never in "
                                      "ModOrganizer.ini (which Wabbajack can ship with a list).")
        row.addWidget(self._chk_remember)
        self._btn_json_nexus = QPushButton("Get collection.json")
        self._btn_json_file = QPushButton("Load collection.json...")
        row.addWidget(self._btn_json_nexus)
        row.addWidget(self._btn_json_file)
        layout.addLayout(row)

        self._info = QLabel("No collection loaded.")
        self._info.setWordWrap(True)
        self._info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._info)

        row = QHBoxLayout()
        self._btn_all = QPushButton("Select all optional")
        self._btn_none = QPushButton("Clear optional")
        self._btn_rescan = QPushButton("Rescan local")
        for b in (self._btn_all, self._btn_none, self._btn_rescan):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(QLabel("Show:"))
        self._filter = QComboBox()
        self._filter.addItems(["Everything", "Mandatory", "Optional", "Selected and missing",
                               "Problems", "Needs manual steps"])
        row.addWidget(self._filter)
        layout.addLayout(row)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        row = QHBoxLayout()
        self._btn_check = QPushButton("Check availability")
        self._btn_download = QPushButton("Download missing (Premium)")
        self._btn_pages = QPushButton("Open next download pages")
        self._spin_pages = QSpinBox()
        self._spin_pages.setRange(1, 25)
        self._spin_pages.setValue(5)
        self._spin_pages.setToolTip("How many 'Mod Manager Download' pages to open per click.")
        self._btn_links = QPushButton("Copy manual links")
        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setEnabled(False)
        self._btn_save = QPushButton("Save selection")
        for w in (self._btn_check, self._btn_download, self._btn_pages, self._spin_pages,
                  self._btn_links, self._btn_stop):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self._btn_save)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("New profile:"))
        self._edit_profile = QLineEdit()
        self._edit_profile.setPlaceholderText("Collection - <name>")
        row.addWidget(self._edit_profile, 1)
        self._chk_separators = QCheckBox("Category separators")
        self._chk_separators.setChecked(True)
        self._chk_reinstall = QCheckBox("Reinstall mods already installed")
        self._chk_mark = QCheckBox("Mark used downloads as installed")
        self._chk_mark.setChecked(True)
        self._chk_mark.setToolTip("After the build, flag every download an installed mod came from "
                                  "as installed (backed up; see the Download matching tab).")
        self._chk_md5 = QCheckBox("Verify archive MD5")
        self._chk_md5.setToolTip("Hash every archive against the curator's MD5 (slow for big lists).")
        for w in (self._chk_separators, self._chk_reinstall, self._chk_mark, self._chk_md5):
            row.addWidget(w)
        self._btn_build = QPushButton("Install && build profile")
        self._btn_build.setToolTip(
            "Installs every selected archive straight into a mod folder - FOMOD options are read "
            "from the archive and the curator's recorded choices applied, no installer windows - "
            "then writes a new profile with the curator's mod order, separators and plugin list.")
        row.addWidget(self._btn_build)
        self._btn_rb = QPushButton("Install Root Builder")
        self._btn_rb.setToolTip("Needed for mods that deploy to the game folder (Root/).")
        self._btn_rb.clicked.connect(lambda: self._offer_rb(""))
        self._btn_rb.setVisible(False)
        row.addWidget(self._btn_rb)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.addStretch(1)
        self._btn_exes = QPushButton("Add programs to MO2…")
        self._btn_exes.setToolTip("SKSE, BodySlide, Pandora, xEdit… found in the collection's "
                                  "installed mods, plus the curator's tools.")
        self._btn_exes.clicked.connect(self._guard(self._offer_exes))
        self._btn_move = QPushButton("Move mods to Root…")
        self._btn_move.setToolTip("Move engine injectors / ENB / preloaders (or whole mods) into "
                                  "Root/ for Root Builder - works on any installed mod.")
        self._btn_move.clicked.connect(self._open_move_dialog)
        row.addWidget(self._btn_exes)
        row.addWidget(self._btn_move)
        layout.addLayout(row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._btn_load.clicked.connect(self._guard(self._load_nexus))
        self._btn_json_nexus.clicked.connect(self._guard(self._get_json_from_nexus))
        self._btn_json_file.clicked.connect(self._guard(self._load_json_file))
        self._btn_all.clicked.connect(lambda: self._set_all_optional(True))
        self._btn_none.clicked.connect(lambda: self._set_all_optional(False))
        self._btn_rescan.clicked.connect(self._guard(self._refresh))
        self._filter.currentIndexChanged.connect(self._apply_filter)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.doubleClicked.connect(self._open_row)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._row_menu)
        self._btn_check.clicked.connect(self._guard(self._check))
        self._btn_download.clicked.connect(self._guard(self._download))
        self._btn_pages.clicked.connect(self._open_next_pages)
        self._btn_links.clicked.connect(self._copy_links)
        self._btn_stop.clicked.connect(self.stop)
        self._btn_save.clicked.connect(self._save_selection)
        self._btn_build.clicked.connect(self._guard(self._build))
        self._set_loaded(False)

    def _guard(self, fn):
        def run() -> None:
            try:
                fn()
            except Exception as exc:
                self._log(f"Collection import: {exc}", force=True)
                QMessageBox.warning(self, PLUGIN_DISPLAY, f"{exc}")
        return run

    def _set_loaded(self, loaded: bool) -> None:
        for w in (self._btn_all, self._btn_none, self._btn_rescan, self._btn_check, self._btn_download,
                  self._btn_pages, self._btn_links, self._btn_save, self._btn_json_nexus,
                  self._btn_build):
            w.setEnabled(loaded)
        self._btn_json_file.setEnabled(True)

    # -- Nexus API ------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {"Application-Name": APP_NAME,
                   "Application-Version": ".".join(str(v) for v in PLUGIN_VERSION)}
        key = self._edit_key.text().strip()
        if key:
            headers["apikey"] = key
        return headers

    def _remember_key(self) -> None:
        key = self._edit_key.text().strip()
        if self._chk_remember.isChecked() and key:
            save_key(key)
        elif not self._chk_remember.isChecked():
            forget_key()

    def _load_nexus(self) -> None:
        domain, slug, rev = C.parse_collection_ref(self._edit_url.text())
        if not slug:
            raise ValueError("Paste a collection URL (…/collections/<slug>) or the slug itself.")
        if rev:
            self._spin_rev.setValue(rev)
        self._remember_key()
        self._slug = slug
        self._fields = list(C.REVISION_FIELDS)
        self._repairs = []
        self._status.setText(f"Asking Nexus for collection {slug}…")
        self._btn_load.setEnabled(False)
        self._query()

    def _query(self) -> None:
        body = C.graphql_request(self._slug, self._spin_rev.value() or None, self._fields)
        self._http.post_json(C.GRAPHQL_URL, body, self._on_graph, headers=self._headers())

    def _on_graph(self, code: Optional[int], text: Optional[str], _ctype: str) -> None:
        try:
            payload = json.loads(text or "{}")
        except ValueError:
            payload = {}
        errors = C.graphql_errors(payload)
        if errors and not ((payload.get("data") or {}).get("collectionRevision")):
            fields, removed = C.strip_failing_fields(self._fields, errors)
            if removed and len(self._repairs) < MAX_QUERY_REPAIRS:
                self._repairs += removed
                self._fields = fields
                self._log(f"Nexus rejected field(s) {', '.join(removed)}; retrying without them", force=True)
                self._query()
                return
        self._btn_load.setEnabled(True)
        if code in (401, 403) or any("auth" in e.lower() for e in errors):
            self._status.setText("Nexus refused the request - add your API key (adult-content and "
                                 "unlisted collections need it).")
            return
        try:
            self._api = C.parse_graphql(payload, self._slug)
        except ValueError as exc:
            self._status.setText(f"Could not load the collection (HTTP {code}): {exc}")
            return
        if self._repairs:
            self._log("Collection loaded without: " + ", ".join(self._repairs), force=True)
        if self._json is not None and self._json.name and self._api.name and self._json.name != self._api.name:
            self._json = None   # a collection.json from a different collection
        self._rebuild()

    def _get_json_from_nexus(self) -> None:
        if self._api is None or not self._api.download_link:
            raise ValueError("Load the collection from Nexus first.")
        if not self._edit_key.text().strip():
            raise ValueError("Downloading the collection archive needs your Nexus API key.")
        self._remember_key()
        link = self._api.download_link
        url = link if link.startswith("http") else C.API_BASE + link
        self._status.setText("Requesting the collection archive link…")
        self._http.get(url, self._on_archive_link, headers=self._headers())

    def _on_archive_link(self, code, text, _ctype) -> None:
        try:
            data = json.loads(text or "{}")
        except ValueError:
            data = {}
        links = data.get("download_links") or ([data["download_link"]] if data.get("download_link") else [])
        uri = ""
        for item in links:
            if isinstance(item, dict):
                uri = item.get("URI") or item.get("uri") or item.get("url") or ""
            elif isinstance(item, str):
                uri = item
            if uri:
                break
        if not uri:
            self._status.setText(f"Nexus gave no archive link (HTTP {code}). "
                                 f"{data.get('message') or data.get('error') or ''}".strip())
            return
        folder = os.path.join(self._data_dir(), "collections")
        os.makedirs(folder, exist_ok=True)
        coll = self._api
        path = os.path.join(folder, f"{coll.slug}-r{coll.revision or 'latest'}.7z")
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._status.setText("Downloading the collection archive…")

        def progress(got: int, total: int) -> None:
            if total > 0:
                self._progress.setRange(0, total)
                self._progress.setValue(got)
                self._progress.setFormat(f"{human_size(got)} / {human_size(total)}")

        self._http.download(uri, path, progress, lambda ok, info: self._on_archive(ok, info))

    def _on_archive(self, ok: bool, info: str) -> None:
        self._progress.setVisible(False)
        if not ok:
            self._status.setText(f"Collection archive download failed: {info}")
            return
        seven = find_7zip()
        if not seven:
            QMessageBox.information(
                self, PLUGIN_DISPLAY,
                f"Saved the collection archive to:\n{info}\n\n7-Zip was not found, so open it "
                "yourself, extract collection.json, and use 'Load collection.json…'.")
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(info)))
            return
        out_dir = os.path.splitext(info)[0]
        os.makedirs(out_dir, exist_ok=True)
        self._status.setText("Extracting collection.json…")
        proc = QProcess(self)
        proc.finished.connect(lambda code, _status: self._on_extracted(proc, code, out_dir, info))
        proc.start(seven, ["x", info, f"-o{out_dir}", "-y"])

    def _on_extracted(self, proc: QProcess, code: int, out_dir: str, archive: str) -> None:
        path = os.path.join(out_dir, "collection.json")
        if code != 0 or not os.path.isfile(path):
            err = bytes(proc.readAllStandardError()).decode("utf-8", "replace").strip()
            self._status.setText(f"7-Zip could not extract collection.json ({code}). {err}")
            return
        self._load_json_path(path)
        self._status.setText(f"Loaded collection.json from {os.path.basename(archive)}. Bundled "
                             f"files and patches stay in {out_dir}'s archive for the install step.")

    def _load_json_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "collection.json", self._data_dir(),
                                              "collection.json (collection.json *.json)")
        if path:
            self._load_json_path(path)

    def _load_json_path(self, path: str) -> None:
        with open(path, "r", encoding="utf-8-sig") as handle:
            self._json = C.parse_collection_json(handle.read())
        self._bundle_root = os.path.dirname(path)
        self._log(f"collection.json: {len(self._json.mods)} mods, {len(self._json.rules)} rules "
                  f"({self._json.unresolved_rules} unresolved)", force=True)
        self._rebuild()

    # -- model ----------------------------------------------------------------

    def _mo2_game(self) -> str:
        try:
            game = self._organizer.managedGame()
            if self._coll and self._coll.domain and game.gameNexusName().lower() == self._coll.domain:
                return game.gameShortName()
        except Exception:
            pass
        return self._coll.domain if self._coll else ""

    def _rebuild(self) -> None:
        self._coll = C.merge(self._api, self._json)
        if not self._coll.mods:
            self._set_loaded(False)
            return
        saved = C.load_selection(self._data_dir(), self._coll)
        if saved is not None:
            self._chosen = saved
        self._root_override = C.load_root_override(self._data_dir(), self._coll)
        self._availability.clear()
        self._opened.clear()
        self._set_loaded(True)
        self._btn_json_nexus.setEnabled(self._api is not None)
        if not self._edit_profile.text().strip():
            self._edit_profile.setText(f"Collection - {self._coll.name or self._coll.slug}")
        self._refresh()

    def _refresh(self) -> None:
        if self._coll is None:
            return
        downloads = model.scan_download_folder(self._organizer.downloadsPath())
        try:
            short = self._organizer.managedGame().gameShortName()
        except Exception:
            short = ""
        mods = model.scan_mods_folder(self._organizer.modsPath(), short)
        C.annotate_local(self._coll, model.match_downloads(downloads, mods))
        self._recompute()

    def _recompute(self) -> None:
        coll = self._coll
        self._selected, self._pulled = C.closure(coll, self._chosen)
        self._populate()
        self._update_info()

    def _update_info(self) -> None:
        coll = self._coll
        lines = [f"<b>{coll.name or coll.slug}</b>"
                 + (f" by {coll.author}" if coll.author else "")
                 + (f" - revision {coll.revision}" if coll.revision else "")
                 + (f" - {human_size(coll.total_size)}" if coll.total_size else "")]
        lines.append(f"{len(coll.mandatory)} mandatory, {len(coll.optional_mods)} optional; "
                     f"{len(self._selected)} selected"
                     + (f" ({len(self._pulled)} pulled in by requirements)" if self._pulled else ""))
        warnings = []
        try:
            game = self._organizer.managedGame()
            if coll.domain and game.gameNexusName().lower() != coll.domain:
                warnings.append(f"collection is for '{coll.domain}', MO2 manages '{game.gameNexusName()}'")
            version = game.gameVersion()
            if coll.game_versions and version and not any(version.startswith(v) or v.startswith(version)
                                                          for v in coll.game_versions):
                warnings.append(f"collection targets game {', '.join(coll.game_versions)}; "
                                f"MO2's game is {version}")
        except Exception:
            pass
        if not coll.has_json:
            warnings.append("no collection.json yet - requirements between mods, FOMOD choices, "
                            "bundled files and patches are unknown")
        else:
            bundled = sum(1 for m in coll.mods if m.source_type == C.SRC_BUNDLE)
            patched = sum(1 for m in coll.mods if m.has_patches)
            fomod = sum(1 for m in coll.mods if m.has_choices)
            lines.append(f"{len(coll.rules)} curator rules ({coll.unresolved_rules} unresolved), "
                         f"{fomod} FOMOD choice sets, {patched} patched mods, {bundled} bundled entries")
        root_mods = [m for m in coll.mods if m.key in self._selected
                     and m.mod_type.lower() in I.ROOT_MOD_TYPES]
        rb_ok = self._rb_installed()
        self._btn_rb.setVisible(not rb_ok)
        if root_mods and not rb_ok:
            warnings.append(f"{len(root_mods)} selected mod(s) deploy to the game folder "
                            f"({', '.join(m.name for m in root_mods[:3])}{'…' if len(root_mods) > 3 else ''}) - "
                            "Root Builder is needed and not installed")
        for a, b in C.conflicts_in(coll, self._selected):
            warnings.append(f"curator marks '{a}' as conflicting with '{b}'")
        if coll.install_info:
            lines.append("Curator notes: " + coll.install_info[:400].replace("\n", " "))
        if warnings:
            lines.append("<span style='color:#d08a2c'>⚠ " + "<br>⚠ ".join(warnings) + "</span>")
        self._info.setText("<br>".join(lines))
        missing = [m for m in coll.mods if m.key in self._selected and m.local == C.LOCAL_MISSING]
        self._status.setText(f"{len(missing)} selected file(s) missing locally.")

    # -- table ----------------------------------------------------------------

    def _populate(self) -> None:
        coll = self._coll
        order = sorted(coll.mods, key=lambda m: (m.optional, (m.category or "~").lower(), m.name.lower()))
        self._rows = [m.key for m in order]
        self._row_of = {k: i for i, k in enumerate(self._rows)}
        self._populating = True
        self._table.setRowCount(len(order))
        for row, mod in enumerate(order):
            check = QTableWidgetItem()
            selected = mod.key in self._selected
            check.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
            locked = not mod.optional or (mod.key in self._pulled)
            flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
            if not locked:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            check.setFlags(flags)
            check.setText("required" if not mod.optional else ("pulled in" if mod.key in self._pulled else "optional"))
            if mod.key in self._pulled:
                check.setToolTip("Required by: " + ", ".join(self._pulled[mod.key]))
            local = mod.local + (" (page opened)" if mod.key in self._opened and mod.local == C.LOCAL_MISSING else "")
            values = [
                mod.name, mod.file_name or mod.logical_filename, mod.version, mod.category,
                mod.source_type + (f" {mod.mod_id}/{mod.file_id}" if mod.mod_id else ""),
                local + (f": {mod.local_detail}" if mod.local_detail else ""),
                self._availability.get(mod.key, ""),
                ("→ game root (Root Builder); " if mod.key in self._root_override
                 or mod.mod_type.lower() in I.ROOT_MOD_TYPES else "") + mod.notes,
            ]
            self._table.setItem(row, 0, check)
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                if column == 8 and mod.instructions:
                    item.setToolTip(mod.instructions)
                elif column in (1, 7):
                    item.setToolTip(value)
                self._table.setItem(row, column, item)
            self._paint(row, mod)
        self._populating = False
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._apply_filter()

    def _update_row(self, key: str) -> None:
        row = self._row_of.get(key)
        if row is None or self._coll is None:
            return
        mod = self._coll.by_key()[key]
        self._populating = True
        item = self._table.item(row, 7)
        if item is not None:
            text = self._availability.get(key, "")
            item.setText(text)
            item.setToolTip(text)
        self._paint(row, mod)
        self._populating = False

    def _paint(self, row: int, mod: C.CollectionMod) -> None:
        c = self._colours
        avail = self._availability.get(mod.key, "")
        if avail.startswith("VERIFY") or avail.startswith(ST_DL_FAILED):
            colours = c["flag"]
        elif mod.key not in self._selected:
            colours = c["skip"]
        elif mod.local == C.LOCAL_INSTALLED:
            colours = c["ok"]
        elif mod.local == C.LOCAL_ARCHIVE:
            colours = c["cached"]
        elif mod.local in (C.LOCAL_MANUAL, C.LOCAL_BUNDLED):
            colours = c["busy"]
        else:
            colours = c["error"]
        for column in range(self._table.columnCount()):
            item = self._table.item(row, column)
            if item is not None:
                item.setBackground(colours[0])
                item.setForeground(colours[1])

    def _apply_filter(self) -> None:
        if self._coll is None:
            return
        mods = self._coll.by_key()
        mode = self._filter.currentIndex()
        for row, key in enumerate(self._rows):
            m = mods[key]
            avail = self._availability.get(key, "")
            visible = {
                1: not m.optional,
                2: m.optional,
                3: key in self._selected and m.local == C.LOCAL_MISSING,
                4: avail.startswith(("VERIFY", "ERROR", ST_DL_FAILED, "FAILED")) or "review" in avail,
                5: key in self._selected and (m.local in (C.LOCAL_MANUAL, C.LOCAL_BUNDLED)
                                              or bool(m.instructions) or m.has_choices),
            }.get(mode, True)
            self._table.setRowHidden(row, not visible)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._populating or item.column() != 0 or self._coll is None:
            return
        key = self._rows[item.row()]
        if item.checkState() == Qt.CheckState.Checked:
            self._chosen.add(key)
        else:
            self._chosen.discard(key)
        self._recompute()

    def _set_all_optional(self, on: bool) -> None:
        if self._coll is None:
            return
        self._chosen = {m.key for m in self._coll.optional_mods} if on else set()
        self._recompute()

    def _open_row(self) -> None:
        rows = {i.row() for i in self._table.selectedIndexes()}
        mods = self._coll.by_key() if self._coll else {}
        for row in sorted(rows)[:10]:
            mod = mods.get(self._rows[row])
            if mod is None:
                continue
            url = C.nexus_file_page(mod, self._coll.domain) if mod.mod_id else mod.url
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _save_selection(self) -> None:
        if self._coll is None:
            return
        path = C.save_selection(self._data_dir(), self._coll, self._chosen, self._root_override)
        self._status.setText(f"Saved selection to {path}")

    # -- availability -----------------------------------------------------------

    def _selected_mods(self, only_missing: bool = False) -> List[C.CollectionMod]:
        by_key = self._coll.by_key()
        return [by_key[k] for k in self._rows if k in self._selected
                and (not only_missing or by_key[k].local == C.LOCAL_MISSING)]

    def _check(self) -> None:
        mo2_game = self._mo2_game()
        self._entries, self._entry_keys = [], []
        for mod in self._selected_mods():
            entry = C.to_entry(mod, mo2_game)
            if entry is not None and entry.kind == KIND_NEXUS:
                self._entries.append(entry)
                self._entry_keys.append(mod.key)
        if not self._entries:
            self._status.setText("No Nexus files selected.")
            return
        self._checker = AvailabilityChecker(self._organizer, self._entries, max_in_flight=4,
                                            verify_file_ids=True, confirm_mod_pages=False, parent=self)
        self._checker.groupResolved.connect(self._on_checked)
        self._checker.progressed.connect(lambda d, t: (self._progress.setRange(0, t), self._progress.setValue(d)))
        self._checker.logged.connect(lambda t: self._log(t, force=True))
        self._checker.debug.connect(self._log)
        self._checker.finished.connect(self._on_check_done)
        self._progress.setVisible(True)
        self._progress.setFormat("%v / %m Nexus requests")
        self._btn_stop.setEnabled(True)
        self._btn_check.setEnabled(False)
        self._status.setText(f"Checking {self._checker.total_groups} file(s) on Nexus (one call each)…")
        self._checker.start()

    def _on_checked(self, indices: List[int]) -> None:
        for index in indices:
            entry = self._entries[index]
            key = self._entry_keys[index]
            self._availability[key] = (
                entry.status if entry.status == ST_OK else f"{entry.status}: {entry.reason}")
            self._update_row(key)

    def _on_check_done(self) -> None:
        self._checker = None
        self._apply_filter()
        self._progress.setVisible(False)
        self._btn_stop.setEnabled(False)
        self._btn_check.setEnabled(True)
        bad = sum(1 for v in self._availability.values() if v.startswith("VERIFY"))
        self._status.setText(f"Availability done: {bad} file(s) no longer served by Nexus."
                             + (" The curator needs to update the collection before it can be "
                                "converted." if bad else ""))
        if bad:
            self._filter.setCurrentIndex(4)

    # -- downloads --------------------------------------------------------------

    def _download(self) -> None:
        if self.runner is not None:
            raise ValueError("A download run is already active.")
        mo2_game = self._mo2_game()
        try:
            managed = self._organizer.managedGame().gameShortName()
        except Exception:
            managed = ""
        self._entries, self._entry_keys = [], []
        for mod in self._selected_mods(only_missing=True):
            if self._availability.get(mod.key, "").startswith("VERIFY"):
                continue
            entry = C.to_entry(mod, mo2_game)
            if entry is None or entry.kind not in (KIND_NEXUS, KIND_DIRECT):
                continue
            if entry.kind == KIND_NEXUS and entry.game != managed:
                continue
            self._entries.append(entry)
            self._entry_keys.append(mod.key)
        if not self._entries:
            self._status.setText("Nothing to queue: every selected file is local, flagged, manual, "
                                 "or for another game.")
            return
        nexus = sum(1 for e in self._entries if e.kind == KIND_NEXUS)
        if QMessageBox.question(
                self, PLUGIN_DISPLAY,
                f"Queue {len(self._entries)} download(s) in MO2 ({nexus} Nexus, "
                f"{len(self._entries) - nexus} direct)?\n\nNexus API downloads are Premium-only; "
                "without Premium use 'Open next download pages' instead.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        self._ensure_hooks()
        self.runner = DownloadRunner(self._organizer, self._entries, list(range(len(self._entries))),
                                     max_active=2, restore_meta=True, parent=self)
        self.runner.entryResolved.connect(self._on_download_update)
        self.runner.progressed.connect(lambda d, t: (self._progress.setRange(0, t), self._progress.setValue(d)))
        self.runner.logged.connect(lambda t: self._log(t, force=True))
        self.runner.debug.connect(self._log)
        self.runner.finished.connect(self._on_downloads_done)
        self._progress.setVisible(True)
        self._progress.setFormat("%v / %m archives")
        self._btn_stop.setEnabled(True)
        self.runner.start()

    def _on_download_update(self, indices: List[int]) -> None:
        for index in indices:
            entry = self._entries[index]
            key = self._entry_keys[index]
            self._availability[key] = f"{entry.status}: {entry.reason}"
            self._update_row(key)

    def _on_downloads_done(self) -> None:
        self.runner = None
        self._progress.setVisible(False)
        self._btn_stop.setEnabled(False)
        self._refresh()

    def _open_next_pages(self) -> None:
        if self._coll is None:
            return
        pending = [m for m in self._selected_mods(only_missing=True)
                   if m.source_type == C.SRC_NEXUS and m.mod_id and m.key not in self._opened
                   and not self._availability.get(m.key, "").startswith("VERIFY")]
        batch = pending[: self._spin_pages.value()]
        for mod in batch:
            QDesktopServices.openUrl(QUrl(C.nexus_file_page(mod, self._coll.domain)))
            self._opened.add(mod.key)
        self._populate()
        self._status.setText(
            f"Opened {len(batch)} page(s); {len(pending) - len(batch)} left. Click 'Mod Manager "
            "Download' on each - MO2 picks up the nxm:// link. Use 'Rescan local' when they land.")

    def _copy_links(self) -> None:
        if self._coll is None:
            return
        lines = []
        for mod in self._selected_mods(only_missing=True):
            url = C.nexus_file_page(mod, self._coll.domain) if mod.mod_id else mod.url
            if url:
                lines.append(f"{mod.name}\t{url}")
        QApplication.clipboard().setText("\n".join(lines))
        self._status.setText(f"Copied {len(lines)} link(s).")

    # -- install & build ----------------------------------------------------------

    def _build(self) -> None:
        if self._coll is None or self._build_thread is not None:
            return
        seven = find_7zip()
        if not seven:
            raise ValueError("7-Zip is needed to extract mod archives (7z, zip and rar). Install "
                             "it from 7-zip.org, then try again.")
        if not self._coll.has_json and QMessageBox.question(
                self, PLUGIN_DISPLAY,
                "No collection.json is loaded, so the curator's FOMOD choices and mod rules are "
                "unknown: FOMOD mods would get their installer defaults and order would be by "
                "category only.\n\nBuild anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        self._refresh()
        game = self._organizer.managedGame()
        profiles_dir = os.path.dirname(self._organizer.profilePath())
        wanted = self._edit_profile.text().strip() or f"Collection - {self._coll.name}"
        plan = B.plan_build(
            self._coll, self._selected,
            downloads_path=self._organizer.downloadsPath(), mods_path=self._organizer.modsPath(),
            profiles_dir=profiles_dir, profile_name=B.unique_profile_name(profiles_dir, wanted),
            game=game.gameShortName(), overwrite=self._chk_reinstall.isChecked(),
            bundle_root=self._bundle_root,
            root_override=self._root_override,
            copy_settings_from=self._organizer.profilePath(),
            use_separators=self._chk_separators.isChecked(),
            game_data_dir=game.dataDirectory().absolutePath(),
            work_dir=os.path.join(self._data_dir(), "work"),
            verify_md5=self._chk_md5.isChecked(),
        )
        existing = len(plan.folder_of) - len(plan.jobs)
        missing = sum(1 for why in plan.skipped.values() if why.startswith("archive not"))
        manual = len(plan.skipped) - missing
        text = (f"Build profile '{plan.profile_name}'?\n\n"
                f"  - {len(plan.jobs)} mod(s) to install from archives\n"
                f"  - {existing} already installed (reused)\n"
                f"  - {missing} skipped: archive not downloaded yet\n"
                f"  - {manual} skipped: manual / bundled not found\n\n"
                f"Mods go into {self._organizer.modsPath()}. Your current profile is not touched; "
                "settings and INIs are copied from it into the new profile.")
        if missing:
            text += "\n\nTip: fetch the missing archives first, or build now and rebuild later."
        if not self._rb_installed():
            text += ("\n\nRoot Builder is not installed: mods that deploy to the game folder (SKSE "
                     "loader, ENB, preloaders) will be placed in their Root/ folder but will not reach "
                     "the game until it is.")
        if QMessageBox.question(self, PLUGIN_DISPLAY, text,
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        self._build_plan = plan
        self._build_thread = BuildThread(plan, I.SevenZipExtractor(seven), self)
        self._build_thread.progressed.connect(self._on_build_progress)
        self._build_thread.installed.connect(self._on_build_result)
        self._build_thread.done.connect(self._on_build_done)
        self._progress.setVisible(True)
        self._progress.setRange(0, max(1, len(plan.jobs)))
        self._progress.setValue(0)
        self._btn_build.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._build_thread.start()

    def _on_build_progress(self, done: int, total: int, name: str) -> None:
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(done)
        self._progress.setFormat(f"%v / %m  {name}")
        self._status.setText(f"Installing {done}/{total}: {name}")

    def _on_build_result(self, res: I.InstallResult) -> None:
        if res.ok:
            text = f"installed ({res.mode})" + (" - review" if res.notes else "")
        else:
            text = "FAILED: " + "; ".join(res.notes)
        self._availability[res.key] = text
        self._update_row(res.key)
        if res.notes:
            self._log(f"{res.mod_name}: {'; '.join(res.notes)}", force=True)

    def _on_build_done(self, report: Any) -> None:
        thread, plan = self._build_thread, self._build_plan
        self._build_thread = None
        self._progress.setVisible(False)
        self._btn_build.setEnabled(True)
        self._btn_stop.setEnabled(False)
        if isinstance(report, Exception):
            self._status.setText(f"Build failed: {report}")
            QMessageBox.warning(self, PLUGIN_DISPLAY, f"Build failed:\n\n{report}")
            return
        folder = os.path.join(self._data_dir(), "collections")
        os.makedirs(folder, exist_ok=True)
        from datetime import datetime
        report_path = os.path.join(folder, f"{I.safe_folder_name(self._coll.slug or self._coll.name or 'collection')}-build-"
                                           f"{datetime.now():%Y%m%d-%H%M%S}.md")
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(report.markdown(plan))
        try:
            self._organizer.refresh(True)
        except Exception:
            pass
        marked = ""
        if self._chk_mark.isChecked() and report.results:
            try:
                marked = self._mark_downloads()
            except Exception as exc:
                marked = f"marking downloads failed: {exc}"
        self._refresh()
        for key, why in plan.skipped.items():
            self._availability.setdefault(key, f"skipped: {why}")
            self._update_row(key)
        ok = sum(1 for r in report.results if r.ok)
        failed = len(report.results) - ok
        review = sum(1 for r in report.results if r.ok and r.notes)
        summary = (f"Installed {ok}, failed {failed}, {review} to review, "
                   f"{len(report.skipped)} skipped. ")
        if marked:
            summary += marked + ". "
        if report.profile_path:
            summary += (f"Profile '{os.path.basename(report.profile_path)}' written - pick it in MO2's "
                        "profile dropdown (restart MO2 if it is not listed yet).")
        self._status.setText(summary)
        rooted = [r.mod_name for r in report.results if r.ok and any("Root/" in n for n in r.notes)]
        rooted += [plan.folder_of[k] for k in plan.order if k in plan.folder_of and k not in plan.jobs
                   and os.path.isdir(os.path.join(plan.mods_path, plan.folder_of[k], "Root"))]
        needs_rb = bool(rooted) and not self._rb_installed()
        if needs_rb:
            with open(report_path, "a", encoding="utf-8") as handle:
                handle.write("\n## Root Builder needed\n\nRoot Builder is not installed; these mods "
                             "have files in Root/ that will not reach the game folder without it:\n\n"
                             + "".join(f"- {name}\n" for name in rooted))
        if needs_rb:
            summary += (f"\n\n{len(rooted)} mod(s) put files in Root/ for the game folder, and Root "
                        "Builder is not installed.")
        box = QMessageBox(self)
        box.setWindowTitle(PLUGIN_DISPLAY)
        box.setText(summary + f"\n\nFull report: {report_path}")
        rb_btn = box.addButton("Install Root Builder", QMessageBox.ButtonRole.ActionRole) if needs_rb else None
        open_btn = box.addButton("Open report", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(report_path))
        elif rb_btn is not None and box.clickedButton() is rb_btn:
            self._offer_rb(f"{len(rooted)} mod(s) in this build need Root Builder: "
                           + ", ".join(rooted[:5]) + ("…" if len(rooted) > 5 else ""))
        # SKSE, BodySlide, Pandora… found in what was just built
        built = [plan.folder_of[k] for k in plan.order if k in plan.folder_of
                 and os.path.isdir(os.path.join(plan.mods_path, plan.folder_of[k]))]
        self._offer_exes(built, quiet=True)

    # -- programs / Root tools ------------------------------------------------------

    def _collection_mod_folders(self) -> List[str]:
        if self._coll is None:
            return []
        mods_path = self._organizer.modsPath()
        folders = []
        for mod in self._coll.mods:
            if mod.key in self._selected and mod.local == C.LOCAL_INSTALLED and mod.local_detail:
                if os.path.isdir(os.path.join(mods_path, mod.local_detail)):
                    folders.append(mod.local_detail)
        return folders

    def _offer_exes(self, folders: Optional[List[str]] = None, quiet: bool = False) -> None:
        folders = folders if folders is not None else self._collection_mod_folders()
        if not folders:
            folders = [n for n in os.listdir(self._organizer.modsPath()) if not n.endswith("_separator")]
        offer_executables(self._organizer, folders, self._data_dir(), self._rb_installed(),
                          self._coll.tools if self._coll else None, self, quiet_if_none=quiet)

    def _open_move_dialog(self) -> None:
        MoveToRootDialog(self._organizer, self._log, self._offer_rb, self._rb_installed, self).exec()
        self._refresh()

    def _row_menu(self, position) -> None:
        if self._coll is None:
            return
        rows = sorted({i.row() for i in self._table.selectedIndexes()})
        keys = [self._rows[r] for r in rows if r < len(self._rows)]
        if not keys:
            return
        menu = QMenu(self)
        all_root = all(k in self._root_override for k in keys)
        toggle = menu.addAction("Install normally (Data)" if all_root else
                                f"Install to game root (Root Builder) - {len(keys)} mod(s)")
        page = menu.addAction("Open download page")
        chosen = menu.exec(self._table.viewport().mapToGlobal(position))
        if chosen is toggle:
            for k in keys:
                (self._root_override.discard if all_root else self._root_override.add)(k)
            C.save_selection(self._data_dir(), self._coll, self._chosen, self._root_override)
            self._populate()
            self._status.setText(
                "These mods will install into Root/ on the next build (reinstall or delete them if "
                "they are already installed)." if not all_root else "Back to a normal Data install.")
        elif chosen is page:
            self._open_row()

    def _mark_downloads(self) -> str:
        downloads = model.scan_download_folder(self._organizer.downloadsPath())
        mods = model.scan_mods_folder(self._organizer.modsPath(),
                                      self._organizer.managedGame().gameShortName())
        plan = model.plan_flag_changes(model.match_downloads(downloads, mods),
                                       hide_matched=False, clear_stale=False)
        if not plan:
            return ""
        changed, _backup, errors = model.apply_flag_changes(
            plan, os.path.join(self._data_dir(), "meta_backups"))
        return (f"{changed} download(s) marked installed (restart MO2 to see it)"
                + (f", {len(errors)} failed" if errors else ""))

    def stop(self) -> None:
        if self._build_thread is not None:
            self._build_thread.request_stop()
        for worker in (self._checker, self.runner):
            if worker is not None:
                try:
                    worker.stop()
                except Exception:
                    pass
        self._http.abort_all()
        self._btn_stop.setEnabled(False)
