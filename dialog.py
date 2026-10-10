# -*- coding: utf-8 -*-
"""The plugin window: Nexus & links / Download matching / Preflight tabs."""

from __future__ import annotations

import csv
import json
import os
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import mobase
from PyQt6.QtCore import QTimer, QUrl, Qt
from PyQt6.QtGui import QColor, QDesktopServices, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt6.QtCore import qInfo, qWarning
except ImportError:  # pragma: no cover
    def qInfo(msg: str) -> None:  # type: ignore
        print(msg)

    def qWarning(msg: str) -> None:  # type: ignore
        print(msg)

from . import model, preflight
from .common import (
    DATA_SUBDIR,
    PLUGIN_DISPLAY,
    PLUGIN_NAME,
    SEV_ERROR,
    SEV_INFO,
    SEV_OK,
    SEV_WARN,
    Finding,
    findings_markdown,
    sort_findings,
)
from .model import (
    FLAGGED_STATES,
    KIND_DIRECT,
    KIND_IPS4,
    KIND_MANUAL,
    KIND_NEXUS,
    ST_CACHED,
    ST_DL_FAILED,
    ST_DOWNLOADED,
    ST_DOWNLOADING,
    ST_ERROR,
    ST_OK,
    ST_PENDING,
    ST_SKIPPED,
    Entry,
    MatchReport,
    archive_missing,
)
from . import rootbuilder as RB
from .collection_tab import CollectionTab
from .workers import HttpFetcher
from .workers import AvailabilityChecker, DownloadRunner, OnlinePreflight, UrlChecker

CACHE_FILE = "verified_cache.json"

# ---------------------------------------------------------------------------
# MO2 helpers
# ---------------------------------------------------------------------------


def managed_game_identifiers(organizer: mobase.IOrganizer) -> Tuple[Set[str], str]:
    """Every name the managed game answers to (meta files say 'SkyrimSE',
    gameName() says 'Skyrim Special Edition')."""
    names: Set[str] = set()
    display = ""
    try:
        game = organizer.managedGame()
    except Exception:
        game = None
    if game is None:
        return names, display
    for accessor in ("gameName", "gameShortName", "gameNexusName"):
        try:
            value = getattr(game, accessor)()
        except Exception:
            continue
        if value:
            names.add(str(value).lower())
            if accessor == "gameName":
                display = str(value)
    for accessor in ("validShortNames", "primarySources"):
        try:
            for value in getattr(game, accessor)() or []:
                if value:
                    names.add(str(value).lower())
        except Exception:
            continue
    return names, display


def game_short_name(organizer: mobase.IOrganizer) -> str:
    try:
        return organizer.managedGame().gameShortName() or ""
    except Exception:
        return ""


def build_context(organizer: mobase.IOrganizer, data_dir: str) -> preflight.Context:
    game = organizer.managedGame()
    game_dir = data = version = ""
    try:
        game_dir = game.gameDirectory().absolutePath()
        data = game.dataDirectory().absolutePath()
    except Exception:
        pass
    try:
        version = game.gameVersion() or ""   # NOT version(): that is the plugin's own version
    except Exception:
        version = ""
    names, _display = managed_game_identifiers(organizer)
    try:
        profiles_dir = os.path.dirname(organizer.profilePath())
    except Exception:
        profiles_dir = os.path.join(organizer.basePath(), "profiles")
    try:
        current = organizer.profileName()
    except Exception:
        current = ""
    return preflight.Context(
        base_path=organizer.basePath(),
        profiles_dir=profiles_dir,
        mods_path=organizer.modsPath(),
        downloads_path=organizer.downloadsPath(),
        overwrite_path=organizer.overwritePath(),
        game_dir=game_dir,
        data_dir=data,
        game_short_name=game_short_name(organizer),
        game_names=names,
        game_version=version,
        current_profile=current,
        plugin_data_dir=data_dir,
    )


def nexus_domain(organizer: mobase.IOrganizer, game_name: str) -> str:
    try:
        game = organizer.getGame(game_name)
        if game is not None and game.gameNexusName():
            return game.gameNexusName()
    except Exception:
        pass
    return game_name.lower()


def mod_url(organizer: mobase.IOrganizer, entry: Entry) -> str:
    if entry.kind != KIND_NEXUS:
        return entry.url
    return f"https://www.nexusmods.com/{nexus_domain(organizer, entry.game)}/mods/{entry.mod_id}"


def manual_download_url(organizer: mobase.IOrganizer, entry: Entry) -> str:
    """nmm=1 hands MO2 an nxm:// link through the browser; works without Premium."""
    if entry.kind != KIND_NEXUS:
        return entry.url
    base = mod_url(organizer, entry)
    return f"{base}?tab=files&file_id={entry.file_id}&nmm=1" if entry.file_id else f"{base}?tab=files"


def download_blocker(entry: Entry, managed_names: Set[str], managed_display: str = "",
                     allow_unverified: bool = False) -> str:
    if entry.flagged:
        return "already flagged as removed"
    if entry.status == ST_SKIPPED:
        return "source type is not supported"
    if entry.status not in (ST_OK, ST_CACHED) and not allow_unverified:
        return "not checked yet - run Check Nexus first, or tick 'fetch unverified'"
    if entry.kind == KIND_DIRECT:
        return "" if entry.url else "no URL in the meta file"
    if entry.kind == KIND_MANUAL:
        return "manualURL downloads have to be done by hand"
    if entry.kind == KIND_IPS4:
        return "ips4 sources are not supported"
    if entry.kind == KIND_NEXUS:
        if not entry.file_id:
            return "no fileID recorded, so the exact file cannot be requested"
        if managed_names and entry.game and entry.game.lower() not in managed_names:
            return (f"belongs to {entry.game}; MO2 can only auto-download for the managed "
                    f"game ({managed_display or 'current game'})")
        return ""
    return "unknown source type"


def write_reports(organizer: mobase.IOrganizer, entries: List[Entry], folder: str) -> List[str]:
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(folder, f"nexus-availability-{stamp}")
    rows = [{
        "status": e.status, "kind": e.kind, "reason": e.reason, "nexus_status": e.nexus_status,
        "source": e.source, "game": e.game, "mod_id": e.mod_id, "file_id": e.file_id or "",
        "mod_name": e.mod_name or e.display_name, "installed_as": "; ".join(e.installed_as),
        "archive": e.archive, "version": e.version, "url": mod_url(organizer, e),
    } for e in entries]
    with open(f"{base}.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["status"])
        writer.writeheader()
        writer.writerows(rows)
    flagged = [r for r in rows if r["status"] in FLAGGED_STATES]
    errors = [r for r in rows if r["status"] == ST_ERROR]
    with open(f"{base}.json", "w", encoding="utf-8") as handle:
        json.dump({"generated": datetime.now().isoformat(timespec="seconds"), "checked": len(rows),
                   "flagged": len(flagged), "entries": rows}, handle, indent=2)
    with open(f"{base}.md", "w", encoding="utf-8") as handle:
        handle.write("# Nexus availability report\n\n")
        handle.write(f"Generated {datetime.now().isoformat(timespec='seconds')}\n\n")
        handle.write(f"- Entries checked: **{len(rows)}**\n- Needing attention: **{len(flagged)}**\n"
                     f"- Inconclusive (network/rate limit): **{len(errors)}**\n\n")
        if flagged:
            handle.write("## Verify Nexus availability\n\n| Mod | Mod ID | File ID | Archive | Reason |\n"
                         "| --- | --- | --- | --- | --- |\n")
            for r in flagged:
                handle.write(f"| [{r['mod_name'] or r['mod_id']}]({r['url']}) | {r['mod_id']} | "
                             f"{r['file_id']} | {r['archive']} | {r['reason']} |\n")
            handle.write("\n")
        if errors:
            handle.write("## Inconclusive - re-run these\n\n")
            for r in errors:
                handle.write(f"- {r['mod_name'] or r['mod_id']} ({r['url']}): {r['reason']}\n")
    return [f"{base}.csv", f"{base}.json", f"{base}.md"]


def _item(text: str, tooltip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if tooltip:
        item.setToolTip(tooltip)
    return item


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class ToolkitDialog(QDialog):
    COLUMNS = ["Status", "Kind", "Mod", "Mod ID", "File ID", "Archive", "Detail"]
    MATCH_COLUMNS = ["Archive", "Installed as", "Matched by", "installed", "hidden", "Planned change"]
    PF_COLUMNS = ["Severity", "Area", "Check"]

    def __init__(self, organizer: mobase.IOrganizer, plugin_name: str, parent=None) -> None:
        super().__init__(parent)
        self._organizer = organizer
        self._plugin_name = plugin_name
        self._entries: List[Entry] = []
        self._checker: Optional[AvailabilityChecker] = None
        self._url_checker: Optional[UrlChecker] = None
        self._downloader: Optional[DownloadRunner] = None
        self._online: Optional[OnlinePreflight] = None
        self._download_hooks = False
        self._cache: Dict[str, Any] = {}
        self._match_report: Optional[MatchReport] = None
        self._match_plan: Dict[str, Dict[str, str]] = {}
        self._findings: List[Finding] = []

        self.setWindowTitle(PLUGIN_DISPLAY)
        self.setMinimumSize(1150, 760)
        self._init_palette()

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_availability_tab(), "Nexus && links")
        self._tabs.addTab(self._build_matching_tab(), "Download matching")
        self._tabs.addTab(self._build_preflight_tab(), "Wabbajack preflight")
        self._rb_http = HttpFetcher(self)
        self._collection_tab = CollectionTab(self._organizer, self._data_dir, self._log,
                                             self._ensure_download_hooks, self._row_colours, self,
                                             root_builder_installed=self._root_builder_installed,
                                             offer_root_builder=self._offer_root_builder)
        self._tabs.addTab(self._collection_tab, "Collection import")
        layout.addWidget(self._tabs, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self._load_cache()

    # -- settings / storage ----------------------------------------------

    def _setting(self, key: str, fallback: Any) -> Any:
        try:
            value = self._organizer.pluginSetting(self._plugin_name, key)
            return fallback if value is None else value
        except Exception:
            return fallback

    def _save_setting(self, key: str, value: Any) -> None:
        try:
            self._organizer.setPluginSetting(self._plugin_name, key, value)
        except Exception:
            pass

    def _data_dir(self) -> str:
        """Per-instance folder in %LOCALAPPDATA%.

        Kept outside the MO2 folder on purpose: a portable instance is
        compiled whole by Wabbajack, so caches, .meta backups, reports and
        collection archives under plugins/data would end up in the list.
        """
        legacy = self._legacy_data_dir()
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return legacy
        import hashlib
        base = os.path.normcase(os.path.abspath(self._organizer.basePath()))
        tag = "".join(ch if ch.isalnum() else "_" for ch in os.path.basename(base))[:40] or "instance"
        tag += "-" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        path = os.path.join(local, "WabbajackAuthorToolkit", tag)
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            old_cache = os.path.join(legacy, CACHE_FILE)
            if os.path.isfile(old_cache):
                try:
                    import shutil
                    shutil.copy2(old_cache, os.path.join(path, CACHE_FILE))
                except OSError:
                    pass
        return path

    def _legacy_data_dir(self) -> str:
        try:
            base = self._organizer.pluginDataPath()
        except Exception:
            base = os.path.join(self._organizer.basePath(), "plugins", "data")
        return os.path.join(base, DATA_SUBDIR)

    def _load_cache(self) -> None:
        path = os.path.join(self._data_dir(), CACHE_FILE)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    self._cache = json.load(handle)
            except Exception:
                self._cache = {}

    def _save_cache(self) -> None:
        try:
            os.makedirs(self._data_dir(), exist_ok=True)
            with open(os.path.join(self._data_dir(), CACHE_FILE), "w", encoding="utf-8") as handle:
                json.dump(self._cache, handle)
        except Exception as exc:
            qWarning(f"[{PLUGIN_NAME}] could not save cache: {exc}")

    def _scan_state(self) -> MatchReport:
        """Downloads + installed mods + matching. Pure file reads, no network."""
        downloads = model.scan_download_folder(self._organizer.downloadsPath())
        mods = model.scan_mods_folder(self._organizer.modsPath(), game_short_name(self._organizer))
        return model.match_downloads(downloads, mods)

    # -- palette ----------------------------------------------------------

    def _init_palette(self) -> None:
        dark = self.palette().color(QPalette.ColorRole.Base).lightness() < 128
        if dark:
            self._row_colours = {
                "flag": (QColor("#4a1f26"), QColor("#ffc9cc")), "error": (QColor("#463818"), QColor("#ffe0a3")),
                "ok": (QColor("#1d3a26"), QColor("#b8e6c2")), "cached": (QColor("#26332c"), QColor("#9dbdaa")),
                "skip": (QColor("#2e2e33"), QColor("#b0b0b8")), "busy": (QColor("#1e3348"), QColor("#bcd9f5")),
            }
        else:
            self._row_colours = {
                "flag": (QColor("#ffe1e1"), QColor("#7a1119")), "error": (QColor("#fff2d4"), QColor("#6b4600")),
                "ok": (QColor("#e3f5e5"), QColor("#12441f")), "cached": (QColor("#eef4ef"), QColor("#3a5442")),
                "skip": (QColor("#f0f0f2"), QColor("#4a4a52")), "busy": (QColor("#e3eefb"), QColor("#13395e")),
            }
        self._sev_colours = {
            SEV_ERROR: self._row_colours["flag"], SEV_WARN: self._row_colours["error"],
            SEV_INFO: self._row_colours["busy"], SEV_OK: self._row_colours["ok"],
        }

    def _paint_row(self, table: QTableWidget, row: int, colours: Optional[Tuple[QColor, QColor]],
                   bold_first: bool = True) -> None:
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is None:
                continue
            if colours is None:
                item.setData(Qt.ItemDataRole.BackgroundRole, None)
                item.setData(Qt.ItemDataRole.ForegroundRole, None)
                continue
            item.setBackground(colours[0])
            item.setForeground(colours[1])
            if column == 0 and bold_first:
                font = table.font()
                font.setBold(True)
                item.setFont(font)

    # =====================================================================
    # Tab 1: Nexus & links
    # =====================================================================

    def _build_availability_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        options = QGroupBox("What to check")
        grid = QGridLayout(options)
        self._chk_downloads = QCheckBox("Downloads folder (.meta files)")
        self._chk_downloads.setChecked(True)
        self._chk_mods = QCheckBox("Installed mods whose archive is gone")
        self._chk_mods.setChecked(bool(self._setting("include_installed_mods", True)))
        self._chk_mods.setToolTip(
            "Installed mods that match an archive in downloads are checked through that "
            "download (one call). Only mods with no archive left are added separately.")
        self._chk_archive_only = QCheckBox("Only entries whose archive is still on disk")
        self._chk_verify_files = QCheckBox("Check by file ID (1 API call per archive)")
        self._chk_verify_files.setChecked(True)
        self._chk_verify_files.setToolTip(
            "Asks Nexus about the exact file Wabbajack will request. A deleted file or a pulled "
            "mod fails this call, so no separate mod-page call is needed.")
        self._chk_confirm_pages = QCheckBox("Also confirm mod page status (+1 call per mod)")
        self._chk_confirm_pages.setChecked(bool(self._setting("confirm_mod_pages", False)))
        self._chk_confirm_pages.setToolTip(
            "Adds a mod-page request so hidden / under-moderation pages are flagged even when "
            "the file itself is still served. Failed files always get a follow-up page "
            "request to record why, so this is only needed for an extra-strict pass.")
        self._chk_links = QCheckBox("Check non-Nexus links (directURL / manualURL)")
        self._chk_links.setChecked(True)
        self._chk_whitelist = QCheckBox("Compare links against Wabbajack's server whitelist")
        self._chk_whitelist.setChecked(True)
        self._chk_restore_meta = QCheckBox("Restore directURL in .meta after downloading")
        self._chk_restore_meta.setChecked(True)
        self._chk_unverified = QCheckBox("Fetch missing archives even if not yet checked")

        grid.addWidget(self._chk_downloads, 0, 0)
        grid.addWidget(self._chk_mods, 0, 1)
        grid.addWidget(self._chk_archive_only, 1, 0)
        grid.addWidget(self._chk_verify_files, 1, 1)
        grid.addWidget(self._chk_links, 2, 0)
        grid.addWidget(self._chk_confirm_pages, 2, 1)
        grid.addWidget(self._chk_whitelist, 3, 0)
        grid.addWidget(self._chk_restore_meta, 3, 1)
        grid.addWidget(self._chk_unverified, 4, 0)

        spins = QHBoxLayout()
        spins.addWidget(QLabel("Simultaneous downloads:"))
        self._spin_downloads = QSpinBox()
        self._spin_downloads.setRange(1, 8)
        self._spin_downloads.setValue(int(self._setting("simultaneous_downloads", 2)))
        spins.addWidget(self._spin_downloads)
        spins.addSpacing(16)
        spins.addWidget(QLabel("Concurrent requests:"))
        self._spin_concurrency = QSpinBox()
        self._spin_concurrency.setRange(1, 16)
        self._spin_concurrency.setValue(int(self._setting("max_concurrent_requests", 4)))
        spins.addWidget(self._spin_concurrency)
        spins.addSpacing(16)
        spins.addWidget(QLabel("Skip entries confirmed OK within (days):"))
        self._spin_cache = QSpinBox()
        self._spin_cache.setRange(0, 365)
        self._spin_cache.setValue(int(self._setting("cache_days", 7)))
        spins.addWidget(self._spin_cache)
        spins.addStretch(1)
        grid.addLayout(spins, 5, 0, 1, 2)
        layout.addWidget(options)

        controls = QHBoxLayout()
        self._btn_scan = QPushButton("Scan MO2")
        self._btn_start = QPushButton("Check Nexus")
        self._btn_stop = QPushButton("Stop")
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(False)
        for b in (self._btn_scan, self._btn_start, self._btn_stop):
            controls.addWidget(b)
        controls.addStretch(1)
        controls.addWidget(QLabel("Show:"))
        self._combo_filter = QComboBox()
        self._combo_filter.addItems(["Everything", "Needs attention", "Errors only", "Archive missing"])
        controls.addWidget(self._combo_filter)
        layout.addLayout(controls)

        self._progress = QProgressBar()
        self._progress.setFormat("%v / %m requests")
        layout.addWidget(self._progress)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._table_menu)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)

        self._status_label = QLabel("Ready. Scan MO2 to build the list.")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        log_row = QHBoxLayout()
        self._chk_verbose = QCheckBox("Verbose log (raw bridge traffic)")
        self._btn_test = QPushButton("Test one request")
        self._btn_clear_log = QPushButton("Clear log")
        log_row.addWidget(self._chk_verbose)
        log_row.addStretch(1)
        log_row.addWidget(self._btn_test)
        log_row.addWidget(self._btn_clear_log)
        layout.addLayout(log_row)
        self._log_pane = QPlainTextEdit()
        self._log_pane.setReadOnly(True)
        self._log_pane.setMaximumHeight(120)
        layout.addWidget(self._log_pane)

        actions = QHBoxLayout()
        self._btn_open = QPushButton("Open selected on Nexus")
        self._btn_copy = QPushButton("Copy flagged URLs")
        self._btn_download = QPushButton("Download missing")
        self._btn_download.setEnabled(False)
        self._btn_manual = QPushButton("Copy manual links")
        self._btn_manual.setEnabled(False)
        self._btn_report = QPushButton("Write report...")
        for b in (self._btn_open, self._btn_copy, self._btn_download, self._btn_manual):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self._btn_report)
        layout.addLayout(actions)

        self._btn_scan.clicked.connect(lambda: self._guard("Scan", self._run_scan))
        self._btn_start.clicked.connect(self._do_check)
        self._btn_test.clicked.connect(self._do_test)
        self._btn_clear_log.clicked.connect(self._log_pane.clear)
        self._btn_stop.clicked.connect(self._do_stop)
        self._btn_open.clicked.connect(self._open_selected)
        self._btn_copy.clicked.connect(self._copy_flagged)
        self._btn_download.clicked.connect(self._do_download)
        self._btn_manual.clicked.connect(self._copy_manual_links)
        self._btn_report.clicked.connect(self._write_report)
        self._combo_filter.currentIndexChanged.connect(self._apply_filter)
        self._table.doubleClicked.connect(self._open_selected)
        return page

    # -- logging ----------------------------------------------------------

    def _log(self, text: str, force: bool = False) -> None:
        if not force and not self._chk_verbose.isChecked() and text.startswith(("<-", "->", "   ")):
            return
        self._log_pane.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
        qInfo(f"[{PLUGIN_NAME}] {text}")

    def _report_exception(self, what: str, exc: BaseException) -> None:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._log(f"{what} FAILED: {exc}", force=True)
        for line in detail.strip().splitlines():
            self._log(f"    {line}", force=True)
        QMessageBox.warning(self, PLUGIN_DISPLAY, f"{what} failed:\n\n{exc}\n\nFull traceback is in "
                            "the log pane on the 'Nexus & links' tab.")

    def _guard(self, what: str, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self._report_exception(what, exc)

    def _do_test(self) -> None:
        nexus = [e for e in self._entries if e.kind == KIND_NEXUS]
        if not nexus:
            QMessageBox.information(self, PLUGIN_DISPLAY, "Scan MO2 first (needs a Nexus entry).")
            return
        entry = nexus[0]
        self._log(f"Test: description for game={entry.game!r} modID={entry.mod_id}", force=True)
        try:
            bridge = self._organizer.createNexusBridge()
            if bridge is None:
                self._log("createNexusBridge() returned None.", force=True)
                return
            self._test_bridge = bridge
            for name in ("descriptionAvailable", "fileInfoAvailable", "requestFailed"):
                try:
                    signal = getattr(bridge, name)
                except (TypeError, AttributeError) as exc:
                    self._log(f"signal '{name}' unusable: {exc}", force=True)
                    continue
                signal.connect(lambda *args, _n=name: self._log(
                    f"<- {_n}: " + ", ".join(f"{type(a).__name__}={a!r}"[:140] for a in args), force=True))
            for user_data in (None, "", 0, []):
                try:
                    bridge.requestDescription(entry.game, entry.mod_id, user_data)
                    self._log(f"request sent with user_data={user_data!r}", force=True)
                    QTimer.singleShot(20000, lambda: self._log(
                        "20s elapsed - if nothing appeared above, MO2 is not dispatching requests "
                        "(check Settings -> Nexus).", force=True))
                    return
                except (TypeError, ValueError) as exc:
                    self._log(f"rejected with user_data={user_data!r}: {exc}", force=True)
            self._log("Could not send any form of the request.", force=True)
        except Exception as exc:
            self._report_exception("Test request", exc)

    # -- scanning ---------------------------------------------------------

    def _run_scan(self) -> None:
        report = self._scan_state()
        self._match_report = report
        entries: List[Entry] = []
        if self._chk_downloads.isChecked():
            entries += model.entries_from_downloads(report, self._chk_archive_only.isChecked())
            if self._chk_mods.isChecked():
                entries += model.entries_from_unmatched_mods(report)
        elif self._chk_mods.isChecked():
            entries += model.entries_from_unmatched_mods(report, report.mods_by_name.values())
        entries = model.dedupe(entries)

        days = self._spin_cache.value()
        cutoff = time.time() - days * 86400 if days > 0 else None
        cached = 0
        if cutoff is not None:
            for entry in entries:
                record = self._cache.get(entry.cache_key)
                if record and float(record.get("checked", 0)) >= cutoff:
                    entry.status, entry.reason = ST_CACHED, "Confirmed available on a previous run"
                    cached += 1

        self._entries = entries
        self._populate_table()
        self._refresh_download_button()

        pending = [e for e in entries if e.status == ST_PENDING]
        links = [e for e in pending if e.kind in (KIND_DIRECT, KIND_MANUAL)]
        plan = model.plan_requests(entries, self._chk_verify_files.isChecked(),
                                   self._chk_confirm_pages.isChecked())
        matched = sum(1 for e in entries if e.installed_as and e.kind == KIND_NEXUS)
        self._btn_start.setEnabled(bool(pending))
        warning = ""
        if plan.total_calls > 2400:
            warning = ("  That is close to the 2500/day Nexus quota - OK results are cached, so it "
                       "can be split across two days.")
        self._status_label.setText(
            f"Found {len(entries)} entries ({cached} skipped from cache; {matched} downloads also "
            f"installed, checked once). Nexus: about {plan.total_calls} API call(s) "
            f"({len(plan.file_groups)} file, {len(plan.need_description)} mod page)."
            + (f" Plus {len(links)} non-Nexus link(s), no API quota." if links else "") + warning)

    # -- table ------------------------------------------------------------

    def _populate_table(self) -> None:
        self._table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            self._fill_row(row, entry)
        self._table.resizeColumnsToContents()
        self._apply_filter()

    def _colour_for(self, entry: Entry) -> Optional[Tuple[QColor, QColor]]:
        c = self._row_colours
        if entry.status == ST_PENDING:
            return None
        return {
            ST_DOWNLOADING: c["busy"], ST_DOWNLOADED: c["ok"], ST_DL_FAILED: c["error"],
            ST_ERROR: c["error"], ST_CACHED: c["cached"], ST_OK: c["ok"], ST_SKIPPED: c["skip"],
        }.get(entry.status, c["flag"] if entry.flagged else None)

    def _fill_row(self, row: int, entry: Entry) -> None:
        values = [
            entry.status, entry.label,
            entry.mod_name or entry.display_name or entry.archive or f"Mod {entry.mod_id}",
            str(entry.mod_id) if entry.mod_id else "-", str(entry.file_id) if entry.file_id else "-",
            entry.archive, entry.reason,
        ]
        for column, value in enumerate(values):
            item = self._table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                self._table.setItem(row, column, item)
            item.setText(value)
            if column == 1 and entry.installed_as:
                item.setToolTip("Installed as: " + ", ".join(entry.installed_as))
            if column == 6:
                item.setToolTip(f"{entry.url}\n\n{value}" if entry.url else value)
        self._paint_row(self._table, row, self._colour_for(entry))

    def _apply_filter(self) -> None:
        mode = self._combo_filter.currentIndex()
        downloads = self._organizer.downloadsPath()
        for row, entry in enumerate(self._entries):
            visible = {
                1: entry.flagged, 2: entry.status == ST_ERROR, 3: archive_missing(entry, downloads),
            }.get(mode, True)
            self._table.setRowHidden(row, not visible)

    # -- checking ---------------------------------------------------------

    def _do_check(self) -> None:
        try:
            self._start_check()
        except Exception as exc:
            self._btn_start.setEnabled(True)
            self._btn_scan.setEnabled(True)
            self._btn_stop.setEnabled(False)
            self._report_exception("Check Nexus", exc)

    def _start_check(self) -> None:
        pending = [e for e in self._entries if e.status == ST_PENDING]
        if not pending:
            QMessageBox.information(self, PLUGIN_DISPLAY, "Nothing to check. Scan first.")
            return
        for key, value in (("max_concurrent_requests", self._spin_concurrency.value()),
                           ("cache_days", self._spin_cache.value()),
                           ("include_installed_mods", self._chk_mods.isChecked()),
                           ("confirm_mod_pages", self._chk_confirm_pages.isChecked())):
            self._save_setting(key, value)

        if not any(e.kind == KIND_NEXUS for e in pending):
            self._on_finished()
            return

        self._checker = AvailabilityChecker(
            organizer=self._organizer, entries=self._entries,
            max_in_flight=self._spin_concurrency.value(),
            timeout_seconds=int(self._setting("request_timeout_seconds", 45)),
            query_status_on_failure=True,
            verify_file_ids=self._chk_verify_files.isChecked(),
            confirm_mod_pages=self._chk_confirm_pages.isChecked(),
            parent=self,
        )
        self._log("Bridge ready, connected signals: " + ", ".join(self._checker.connected_signals), force=True)
        self._checker.progressed.connect(self._on_progress)
        self._checker.groupResolved.connect(self._on_group)
        self._checker.logged.connect(self._on_checker_message)
        self._checker.debug.connect(self._log)
        self._checker.finished.connect(self._on_finished)

        self._progress.setRange(0, max(1, self._checker.total_groups))
        self._progress.setValue(0)
        self._progress.setFormat("%v / %m Nexus requests")
        self._btn_start.setEnabled(False)
        self._btn_scan.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._status_label.setText(f"Querying Nexus through MO2's linked account "
                                   f"({self._checker.total_groups} requests)...")
        self._checker.start()

    def _on_checker_message(self, text: str) -> None:
        self._status_label.setText(text)
        self._log(text, force=True)

    def _do_stop(self) -> None:
        for worker in (self._downloader, self._url_checker, self._checker):
            if worker is not None:
                worker.stop()
        self._btn_stop.setEnabled(False)

    def _on_progress(self, done: int, total: int) -> None:
        self._progress.setValue(done)

    def _on_group(self, indices: List[int]) -> None:
        for index in indices:
            self._fill_row(index, self._entries[index])
        self._apply_filter()

    def _on_finished(self) -> None:
        if not self._start_url_phase():
            self._finalize()

    def _start_url_phase(self) -> bool:
        if not self._chk_links.isChecked():
            return False
        indices = [i for i, e in enumerate(self._entries)
                   if e.kind in (KIND_DIRECT, KIND_MANUAL) and e.status == ST_PENDING and e.url]
        if not indices:
            return False
        try:
            self._url_checker = UrlChecker(
                entries=self._entries, indices=indices, max_in_flight=self._spin_concurrency.value(),
                timeout_seconds=int(self._setting("request_timeout_seconds", 45)),
                check_whitelist=self._chk_whitelist.isChecked(), parent=self)
        except Exception as exc:
            self._report_exception("Link check", exc)
            return False
        self._url_checker.progressed.connect(self._on_progress)
        self._url_checker.entryResolved.connect(self._on_group)
        self._url_checker.logged.connect(self._on_checker_message)
        self._url_checker.debug.connect(self._log)
        self._url_checker.finished.connect(self._finalize)
        self._progress.setRange(0, max(1, len(indices)))
        self._progress.setValue(0)
        self._progress.setFormat("%v / %m links")
        self._btn_stop.setEnabled(True)
        self._btn_scan.setEnabled(False)
        self._status_label.setText(f"Checking {len(indices)} non-Nexus link(s)...")
        self._url_checker.start()
        return True

    def _finalize(self) -> None:
        self._btn_stop.setEnabled(False)
        self._btn_scan.setEnabled(True)
        self._btn_start.setEnabled(any(e.status == ST_PENDING for e in self._entries))
        self._progress.setFormat("%v / %m")
        now = time.time()
        for entry in self._entries:
            if entry.status == ST_OK:
                self._cache[entry.cache_key] = {"checked": now}
            elif entry.flagged:
                self._cache.pop(entry.cache_key, None)
        self._save_cache()
        flagged = sum(1 for e in self._entries if e.flagged)
        errors = sum(1 for e in self._entries if e.status == ST_ERROR)
        self._status_label.setText(
            f"Done. {flagged} entr{'y' if flagged == 1 else 'ies'} need 'verify nexus availability', "
            f"{errors} inconclusive and worth a manual look.")
        self._refresh_download_button()
        if flagged or errors:
            self._combo_filter.setCurrentIndex(1 if flagged else 2)

    # -- fetching missing archives -----------------------------------------

    def _download_candidates(self) -> Tuple[List[int], Dict[str, int]]:
        downloads = self._organizer.downloadsPath()
        names, display = managed_game_identifiers(self._organizer)
        indices: List[int] = []
        blocked: Dict[str, int] = {}
        for index, entry in enumerate(self._entries):
            if entry.status in (ST_DOWNLOADING, ST_DOWNLOADED) or not archive_missing(entry, downloads):
                continue
            reason = download_blocker(entry, names, display, self._chk_unverified.isChecked())
            if reason:
                blocked[reason] = blocked.get(reason, 0) + 1
            else:
                indices.append(index)
        return indices, blocked

    def _missing_indices(self) -> List[int]:
        downloads = self._organizer.downloadsPath()
        return [i for i, e in enumerate(self._entries)
                if e.status not in (ST_DOWNLOADING, ST_DOWNLOADED) and archive_missing(e, downloads)]

    def _refresh_download_button(self) -> None:
        try:
            indices, blocked = self._download_candidates()
            missing = self._missing_indices()
        except Exception:
            indices, blocked, missing = [], {}, []
        self._btn_download.setText(f"Download missing ({len(indices)})")
        self._btn_download.setEnabled(bool(missing) and self._downloader is None)
        self._btn_manual.setText(f"Copy manual links ({len(missing)})")
        self._btn_manual.setEnabled(bool(missing))

    def _ensure_download_hooks(self) -> None:
        """IDownloadManager callbacks are permanent: install once, route to
        whichever runners are active (Nexus tab and Collection tab)."""
        if self._download_hooks:
            return
        manager = self._organizer.downloadManager()
        if manager is None:
            raise RuntimeError("MO2 provided no download manager.")
        manager.onDownloadComplete(lambda did: self._route_download(did, True))
        manager.onDownloadFailed(lambda did: self._route_download(did, False))
        self._download_hooks = True

    def _route_download(self, download_id: int, ok: bool) -> None:
        try:
            if not self.isVisible():
                return
        except RuntimeError:
            return  # this window was closed and destroyed
        runners = [self._downloader, getattr(self, "_collection_tab", None) and self._collection_tab.runner]
        for runner in runners:
            if runner is None:
                continue
            try:
                (runner.handle_complete if ok else runner.handle_failed)(download_id)
            except RuntimeError:
                pass  # runner's Qt object already gone

    def _do_download(self) -> None:
        try:
            self._start_downloads()
        except Exception as exc:
            self._downloader = None
            self._refresh_download_button()
            self._report_exception("Download", exc)

    def _start_downloads(self) -> None:
        indices, blocked = self._download_candidates()
        skipped = "\n".join(f"  - {c} x {r}" for r, c in blocked.items())
        if not indices:
            QMessageBox.information(self, PLUGIN_DISPLAY, "Nothing can be fetched automatically right now."
                                    + (f"\n\nSkipped:\n{skipped}" if skipped else ""))
            return
        nexus_count = sum(1 for i in indices if self._entries[i].kind == KIND_NEXUS)
        message = [f"Queue {len(indices)} missing archive(s) in MO2?", "",
                   f"  - {nexus_count} from Nexus (uses your linked account)",
                   f"  - {len(indices) - nexus_count} from directURL links"]
        if nexus_count:
            message += ["", "Nexus API downloads are Premium-only. Without Premium MO2 will fail "
                        "these; use 'Copy manual links' instead."]
        if skipped:
            message += ["", "Not queued:", skipped]
        if QMessageBox.question(self, PLUGIN_DISPLAY, "\n".join(message),
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        self._launch_runner(indices)

    def _launch_runner(self, indices: List[int]) -> None:
        self._ensure_download_hooks()
        self._save_setting("simultaneous_downloads", self._spin_downloads.value())
        self._downloader = DownloadRunner(self._organizer, self._entries, indices,
                                          max_active=self._spin_downloads.value(),
                                          restore_meta=self._chk_restore_meta.isChecked(), parent=self)
        self._downloader.progressed.connect(self._on_progress)
        self._downloader.entryResolved.connect(self._on_group)
        self._downloader.logged.connect(self._on_checker_message)
        self._downloader.debug.connect(self._log)
        self._downloader.finished.connect(self._on_downloads_finished)
        self._progress.setRange(0, max(1, len(indices)))
        self._progress.setValue(0)
        self._progress.setFormat("%v / %m archives")
        self._btn_download.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._downloader.start()

    def _on_downloads_finished(self) -> None:
        done = sum(1 for e in self._entries if e.status == ST_DOWNLOADED)
        failed = sum(1 for e in self._entries if e.status == ST_DL_FAILED)
        self._downloader = None
        self._btn_stop.setEnabled(False)
        self._status_label.setText(f"Downloads finished: {done} fetched, {failed} failed. "
                                   "Anything still running is in MO2's Downloads tab.")
        self._refresh_download_button()

    def _copy_manual_links(self) -> None:
        lines = []
        for index in self._missing_indices():
            entry = self._entries[index]
            url = manual_download_url(self._organizer, entry)
            if url:
                lines.append(f"{entry.mod_name or entry.display_name or entry.archive}\t{url}")
        if not lines:
            QMessageBox.information(self, PLUGIN_DISPLAY, "No usable links for the missing archives.")
            return
        QApplication.clipboard().setText("\n".join(lines))
        self._status_label.setText(f"Copied {len(lines)} download link(s).")

    def _selected_entries(self) -> List[Entry]:
        rows = {i.row() for i in self._table.selectedIndexes()}
        return [self._entries[r] for r in sorted(rows) if r < len(self._entries)]

    def _table_menu(self, position) -> None:
        entries = self._selected_entries()
        if not entries:
            return
        menu = QMenu(self)
        open_action = menu.addAction(f"Open download page ({len(entries)})")
        copy_action = menu.addAction("Copy download link")
        menu.addSeparator()
        queue_action = menu.addAction("Queue download in MO2")
        meta_action = menu.addAction("Copy .meta path")
        chosen = menu.exec(self._table.viewport().mapToGlobal(position))
        if chosen is open_action:
            self._open_selected()
        elif chosen is copy_action:
            urls = [manual_download_url(self._organizer, e) for e in entries]
            QApplication.clipboard().setText("\n".join(u for u in urls if u))
        elif chosen is meta_action:
            QApplication.clipboard().setText("\n".join(e.meta_path for e in entries if e.meta_path))
        elif chosen is queue_action:
            self._queue_specific(entries)

    def _queue_specific(self, entries: List[Entry]) -> None:
        if self._downloader is not None:
            QMessageBox.information(self, PLUGIN_DISPLAY, "A download run is already active.")
            return
        names, display = managed_game_identifiers(self._organizer)
        indices, blocked = [], []
        for entry in entries:
            reason = download_blocker(entry, names, display, allow_unverified=True)
            if reason:
                blocked.append(f"{entry.mod_name or entry.archive}: {reason}")
            else:
                indices.append(self._entries.index(entry))
        if not indices:
            QMessageBox.information(self, PLUGIN_DISPLAY, "None of the selected rows can be "
                                    "auto-downloaded:\n\n" + "\n".join(blocked[:12]))
            return
        self._launch_runner(indices)

    def _open_selected(self) -> None:
        for entry in self._selected_entries()[:10]:
            url = manual_download_url(self._organizer, entry)
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _copy_flagged(self) -> None:
        urls = sorted({mod_url(self._organizer, e) for e in self._entries if e.flagged})
        if not urls:
            QMessageBox.information(self, PLUGIN_DISPLAY, "Nothing flagged.")
            return
        QApplication.clipboard().setText("\n".join(urls))
        self._status_label.setText(f"Copied {len(urls)} URL(s).")

    def _write_report(self) -> None:
        if not self._entries:
            QMessageBox.information(self, PLUGIN_DISPLAY, "Nothing to report yet.")
            return
        os.makedirs(self._data_dir(), exist_ok=True)
        folder = QFileDialog.getExistingDirectory(self, "Where should the report go?", self._data_dir())
        if not folder:
            return
        try:
            written = write_reports(self._organizer, self._entries, folder)
        except Exception as exc:
            QMessageBox.warning(self, PLUGIN_DISPLAY, f"Could not write the report:\n{exc}")
            return
        self._status_label.setText(f"Wrote {len(written)} file(s) to {folder}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # =====================================================================
    # Tab 2: Download matching
    # =====================================================================

    def _build_matching_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel(
            "Pairs every archive in downloads with the installed mod(s) built from it - by "
            "archive name (meta.ini installationFile) or by Nexus mod/file ID - without touching "
            "Nexus. Matched downloads can be flagged as installed (and optionally hidden) so "
            "MO2's Downloads tab reflects what the list actually uses.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        opts = QHBoxLayout()
        self._chk_m_hide = QCheckBox("Also hide matched downloads")
        self._chk_m_hide.setToolTip("Sets removed=true, MO2's 'Hide' state. Wabbajack still sees hidden archives.")
        self._chk_m_stale = QCheckBox("Clear 'installed' on downloads no mod uses")
        for w in (self._chk_m_hide, self._chk_m_stale):
            opts.addWidget(w)
            w.toggled.connect(self._replan_matching)
        opts.addStretch(1)
        opts.addWidget(QLabel("Show:"))
        self._combo_m_filter = QComboBox()
        self._combo_m_filter.addItems(["Everything", "Matched", "Not matched", "Will change",
                                       "Problems (no .meta / no source / orphan)"])
        self._combo_m_filter.currentIndexChanged.connect(self._apply_match_filter)
        opts.addWidget(self._combo_m_filter)
        layout.addLayout(opts)

        buttons = QHBoxLayout()
        self._btn_m_scan = QPushButton("Scan")
        self._btn_m_apply = QPushButton("Apply flags")
        self._btn_m_apply.setEnabled(False)
        self._btn_m_restore = QPushButton("Restore last backup")
        self._btn_m_backups = QPushButton("Open backup folder")
        for b in (self._btn_m_scan, self._btn_m_apply, self._btn_m_restore, self._btn_m_backups):
            buttons.addWidget(b)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self._m_table = QTableWidget(0, len(self.MATCH_COLUMNS))
        self._m_table.setHorizontalHeaderLabels(self.MATCH_COLUMNS)
        self._m_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._m_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        h = self._m_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self._m_table)

        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        self._m_unmatched_label = QLabel("Installed mods with no archive in downloads")
        lower_layout.addWidget(self._m_unmatched_label)
        self._m_unmatched = QListWidget()
        lower_layout.addWidget(self._m_unmatched)
        splitter.addWidget(lower)
        splitter.setSizes([420, 140])
        layout.addWidget(splitter, 1)

        self._m_status = QLabel("Scan to match downloads to installed mods.")
        self._m_status.setWordWrap(True)
        layout.addWidget(self._m_status)

        self._btn_m_scan.clicked.connect(lambda: self._guard("Match scan", self._run_matching))
        self._btn_m_apply.clicked.connect(lambda: self._guard("Apply flags", self._apply_matching))
        self._btn_m_restore.clicked.connect(lambda: self._guard("Restore", self._restore_matching))
        self._btn_m_backups.clicked.connect(self._open_backups)
        return page

    def _backup_root(self) -> str:
        return os.path.join(self._data_dir(), "meta_backups")

    def _run_matching(self) -> None:
        self._match_report = self._scan_state()
        self._replan_matching()

    def _replan_matching(self) -> None:
        report = self._match_report
        if report is None:
            return
        plan = model.plan_flag_changes(report, self._chk_m_hide.isChecked(), self._chk_m_stale.isChecked())
        self._match_plan = {path: values for path, values, _why in plan}

        self._m_table.setRowCount(len(report.rows))
        for row, r in enumerate(report.rows):
            dl = r.download
            change = self._match_plan.get(dl.meta_path, {})
            problems = []
            if not dl.has_meta:
                problems.append("no .meta")
            elif not dl.has_archive:
                problems.append("orphan .meta")
            elif not dl.has_source:
                problems.append("no source in .meta")
            values = [
                dl.archive,
                ", ".join(r.mods) if r.mods else ("; ".join(problems) if problems else "-"),
                r.how or "-",
                "yes" if dl.installed_flag else "no" if dl.has_meta else "-",
                "yes" if dl.hidden else "no" if dl.has_meta else "-",
                ", ".join(f"{k}={v}" for k, v in change.items()),
            ]
            for column, value in enumerate(values):
                self._m_table.setItem(row, column, _item(value, value if column in (0, 1) else ""))
            colours = (self._row_colours["error"] if problems else
                       self._row_colours["busy"] if change else
                       self._row_colours["ok"] if r.matched else None)
            self._paint_row(self._m_table, row, colours, bold_first=False)
        self._m_table.resizeColumnsToContents()
        self._apply_match_filter()

        self._m_unmatched.clear()
        enabled = self._enabled_in_current_profile()
        for mod in report.unmatched_mods:
            tag = "" if not enabled or mod.name in enabled else "  (disabled)"
            src = f"Nexus {mod.mod_id}" if mod.mod_id else "no Nexus ID"
            archive = mod.installation_file or "no installationFile"
            self._m_unmatched.addItem(f"{mod.name}  -  {archive}  -  {src}{tag}")
        self._m_unmatched_label.setText(
            f"Installed mods with no archive in downloads ({len(report.unmatched_mods)}) - "
            "restore the archive or cover them with NoMatchInclude")

        matched = len(report.matched_rows)
        self._btn_m_apply.setEnabled(bool(self._match_plan))
        self._btn_m_apply.setText(f"Apply flags ({len(self._match_plan)})")
        self._m_status.setText(
            f"{len(report.rows)} download entries, {matched} matched to installed mods, "
            f"{len(report.unmatched_mods)} installed mods without an archive. "
            f"{len(self._match_plan)} .meta file(s) would change.")

    def _enabled_in_current_profile(self) -> Set[str]:
        try:
            from .common import read_modlist
            return {m.name for m in read_modlist(self._organizer.profilePath()) if m.enabled}
        except Exception:
            return set()

    def _apply_match_filter(self) -> None:
        report = self._match_report
        if report is None:
            return
        mode = self._combo_m_filter.currentIndex()
        for row, r in enumerate(report.rows):
            dl = r.download
            problem = not dl.has_meta or not dl.has_archive or not dl.has_source
            visible = {
                1: r.matched, 2: not r.matched and dl.has_archive,
                3: dl.meta_path in self._match_plan, 4: problem,
            }.get(mode, True)
            self._m_table.setRowHidden(row, not visible)

    def _apply_matching(self) -> None:
        if not self._match_plan:
            return
        report = self._match_report
        plan = model.plan_flag_changes(report, self._chk_m_hide.isChecked(), self._chk_m_stale.isChecked())
        mark = sum(1 for _p, v, _w in plan if v.get("installed") == "true")
        hide = sum(1 for _p, v, _w in plan if v.get("removed") == "true")
        clear = sum(1 for _p, v, _w in plan if v.get("installed") == "false")
        text = (f"Edit {len(plan)} .meta file(s)?\n\n  - {mark} marked installed\n  - {hide} hidden\n"
                f"  - {clear} 'installed' cleared\n\nEvery file is backed up first, and "
                "'Restore last backup' undoes this run.\n\nMO2 holds download states in memory: "
                "restart MO2 afterwards so its Downloads tab reads the new flags, and avoid "
                "changing these downloads in MO2 before then.")
        if QMessageBox.question(self, PLUGIN_DISPLAY, text,
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        changed, backup, errors = model.apply_flag_changes(plan, self._backup_root())
        msg = f"Updated {changed} .meta file(s). Backup: {backup}"
        if errors:
            msg += f"\n\n{len(errors)} failed:\n" + "\n".join(errors[:15])
        QMessageBox.information(self, PLUGIN_DISPLAY, msg + "\n\nRestart MO2 to see the new states.")
        self._run_matching()

    def _restore_matching(self) -> None:
        backup = model.latest_backup(self._backup_root())
        if not backup:
            QMessageBox.information(self, PLUGIN_DISPLAY, "No backups yet.")
            return
        if QMessageBox.question(self, PLUGIN_DISPLAY, f"Restore every .meta from\n{backup}?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        restored, errors = model.restore_backup(backup)
        QMessageBox.information(self, PLUGIN_DISPLAY, f"Restored {restored} file(s)."
                                + (f"\n\nErrors:\n" + "\n".join(errors[:15]) if errors else "")
                                + "\n\nRestart MO2 to see the restored states.")
        self._run_matching()

    def _open_backups(self) -> None:
        os.makedirs(self._backup_root(), exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._backup_root()))

    # =====================================================================
    # Tab 3: Wabbajack preflight
    # =====================================================================

    def _build_preflight_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        row = QHBoxLayout()
        row.addWidget(QLabel("Compiler settings:"))
        self._pf_combo = QComboBox()
        self._pf_combo.setMinimumWidth(420)
        row.addWidget(self._pf_combo, 1)
        self._btn_pf_browse = QPushButton("Browse...")
        self._btn_pf_open = QPushButton("Open file")
        row.addWidget(self._btn_pf_browse)
        row.addWidget(self._btn_pf_open)
        layout.addLayout(row)

        row2 = QHBoxLayout()
        self._chk_pf_online = QCheckBox("Online checks (repository, modlists.json, links, "
                                        "Wabbajack's validation report)")
        self._chk_pf_online.setChecked(bool(self._setting("preflight_online", True)))
        row2.addWidget(self._chk_pf_online)
        row2.addStretch(1)
        row2.addWidget(QLabel("Show:"))
        self._pf_filter = QComboBox()
        self._pf_filter.addItems(["Everything", "Problems (errors + warnings)", "Errors only"])
        self._pf_filter.currentIndexChanged.connect(self._apply_pf_filter)
        row2.addWidget(self._pf_filter)
        self._btn_pf_run = QPushButton("Run preflight")
        row2.addWidget(self._btn_pf_run)
        layout.addLayout(row2)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self._pf_table = QTableWidget(0, len(self.PF_COLUMNS))
        self._pf_table.setHorizontalHeaderLabels(self.PF_COLUMNS)
        self._pf_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._pf_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._pf_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._pf_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self._pf_table)
        self._pf_detail = QPlainTextEdit()
        self._pf_detail.setReadOnly(True)
        self._pf_detail.setPlaceholderText("Select a row for details and the suggested fix. "
                                           "Double-click opens its file or link.")
        splitter.addWidget(self._pf_detail)
        splitter.setSizes([430, 170])
        layout.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self._pf_status = QLabel("Looks for *.compiler_settings in the MO2 base folder.")
        self._pf_status.setWordWrap(True)
        bottom.addWidget(self._pf_status, 1)
        self._btn_pf_copy = QPushButton("Copy as Markdown")
        self._btn_pf_save = QPushButton("Save report...")
        self._btn_pf_rb = QPushButton("Install Root Builder")
        self._btn_pf_rb.setToolTip("Root Builder deploys mods' Root/ folders into the game folder.")
        self._btn_pf_rb.clicked.connect(lambda: self._offer_root_builder(""))
        bottom.addWidget(self._btn_pf_rb)
        self._btn_pf_move = QPushButton("Move mods to Root…")
        self._btn_pf_move.clicked.connect(self._open_move_root)
        bottom.addWidget(self._btn_pf_move)
        bottom.addWidget(self._btn_pf_copy)
        bottom.addWidget(self._btn_pf_save)
        layout.addLayout(bottom)

        self._btn_pf_browse.clicked.connect(self._pf_browse)
        self._btn_pf_open.clicked.connect(self._pf_open_settings)
        self._btn_pf_run.clicked.connect(lambda: self._guard("Preflight", self._run_preflight))
        self._btn_pf_copy.clicked.connect(self._pf_copy)
        self._btn_pf_save.clicked.connect(self._pf_save)
        self._pf_table.itemSelectionChanged.connect(self._pf_show_detail)
        self._pf_table.doubleClicked.connect(self._pf_open_target)
        self._refresh_settings_list()
        return page

    def _refresh_settings_list(self, select: str = "") -> None:
        try:
            base = self._organizer.basePath()
            found = preflight.find_compiler_settings(base)
            current = self._organizer.profileName()
        except Exception:
            found, current = [], ""
        if select and select not in found:
            found.insert(0, select)
        self._pf_combo.clear()
        for path in found:
            self._pf_combo.addItem(os.path.basename(path), path)
        self._pf_combo.addItem("(none - skip compiler checks)", "")
        chosen = select or preflight.pick_compiler_settings(found, current)
        index = self._pf_combo.findData(chosen)
        self._pf_combo.setCurrentIndex(max(0, index))

    def _pf_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Wabbajack compiler settings", self._organizer.basePath(),
                                              "Compiler settings (*.compiler_settings);;All files (*)")
        if path:
            self._refresh_settings_list(select=path)

    def _pf_open_settings(self) -> None:
        path = self._pf_combo.currentData()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _run_preflight(self) -> None:
        if self._online is not None:
            return
        self._save_setting("preflight_online", self._chk_pf_online.isChecked())
        ctx = build_context(self._organizer, self._data_dir())
        ctx.root_builder = self._root_builder_installed()
        self._btn_pf_rb.setVisible(not ctx.root_builder)
        report = self._scan_state()
        self._match_report = report
        findings: List[Finding] = []
        settings: Optional[Dict[str, Any]] = None
        path = self._pf_combo.currentData() or ""
        if path:
            try:
                settings = preflight.load_compiler_settings(path)
            except Exception as exc:
                findings.append(Finding(SEV_ERROR, "Compiler settings", "Could not read compiler settings",
                                        str(exc), path=path))
        else:
            findings.append(Finding(
                SEV_INFO, "Compiler settings", "No compiler settings selected",
                "Wabbajack saves <Profile>.compiler_settings in the MO2 folder after you set up a "
                "compile once; select it (or Browse) to enable list-level checks."))
        if settings is not None:
            findings += preflight.check_compiler_settings(ctx, path, settings, report)
        findings += preflight.check_instance(ctx, report)
        findings += preflight.check_stock_game(ctx, settings)
        self._findings = findings
        self._populate_pf()

        if settings is not None and self._chk_pf_online.isChecked():
            downloads = {r.download.archive for r in report.rows if r.download.has_archive}
            self._online = OnlinePreflight(settings, downloads, parent=self)
            self._online.progress.connect(self._pf_status.setText)
            self._online.finished.connect(self._on_online_done)
            self._btn_pf_run.setEnabled(False)
            self._online.start()
        else:
            self._pf_summary()

    def _on_online_done(self, findings: List[Finding]) -> None:
        self._findings += findings
        self._online = None
        self._btn_pf_run.setEnabled(True)
        self._populate_pf()
        self._pf_summary()

    def _pf_summary(self) -> None:
        counts = {s: sum(1 for f in self._findings if f.severity == s) for s in (SEV_ERROR, SEV_WARN, SEV_INFO, SEV_OK)}
        self._pf_status.setText(f"{counts[SEV_ERROR]} error(s), {counts[SEV_WARN]} warning(s), "
                                f"{counts[SEV_INFO]} note(s), {counts[SEV_OK]} passed.")
        if counts[SEV_ERROR] or counts[SEV_WARN]:
            self._pf_filter.setCurrentIndex(1)

    def _populate_pf(self) -> None:
        self._findings = sort_findings(self._findings)
        self._pf_table.setRowCount(len(self._findings))
        for row, f in enumerate(self._findings):
            self._pf_table.setItem(row, 0, _item(f.severity))
            self._pf_table.setItem(row, 1, _item(f.area))
            self._pf_table.setItem(row, 2, _item(f.title, f.detail))
            self._paint_row(self._pf_table, row, self._sev_colours.get(f.severity))
        self._pf_table.resizeColumnsToContents()
        self._pf_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._apply_pf_filter()

    def _apply_pf_filter(self) -> None:
        mode = self._pf_filter.currentIndex()
        for row, f in enumerate(self._findings):
            visible = {1: f.severity in (SEV_ERROR, SEV_WARN), 2: f.severity == SEV_ERROR}.get(mode, True)
            self._pf_table.setRowHidden(row, not visible)

    def _pf_selected(self) -> Optional[Finding]:
        rows = {i.row() for i in self._pf_table.selectedIndexes()}
        return self._findings[min(rows)] if rows and min(rows) < len(self._findings) else None

    def _pf_show_detail(self) -> None:
        f = self._pf_selected()
        if f is None:
            self._pf_detail.clear()
            return
        parts = [f"[{f.severity}] {f.area}: {f.title}"]
        if f.detail:
            parts += ["", f.detail]
        if f.fix:
            parts += ["", f"Fix: {f.fix}"]
        if f.path:
            parts += ["", f"Path: {f.path}"]
        if f.url:
            parts += ["", f"Link: {f.url}"]
        self._pf_detail.setPlainText("\n".join(parts))

    def _pf_open_target(self) -> None:
        f = self._pf_selected()
        if f is None:
            return
        if f.url:
            QDesktopServices.openUrl(QUrl(f.url))
        elif f.path:
            target = f.path if os.path.isdir(f.path) else os.path.dirname(f.path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _pf_copy(self) -> None:
        if not self._findings:
            return
        QApplication.clipboard().setText(findings_markdown(self._findings))
        self._pf_status.setText("Copied the preflight report as Markdown.")

    def _pf_save(self) -> None:
        if not self._findings:
            QMessageBox.information(self, PLUGIN_DISPLAY, "Run the preflight first.")
            return
        os.makedirs(self._data_dir(), exist_ok=True)
        default = os.path.join(self._data_dir(), f"wabbajack-preflight-{datetime.now():%Y%m%d-%H%M%S}.md")
        path, _ = QFileDialog.getSaveFileName(self, "Save preflight report", default, "Markdown (*.md)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(findings_markdown(self._findings))
        with open(os.path.splitext(path)[0] + ".json", "w", encoding="utf-8") as handle:
            json.dump([f.as_dict() for f in self._findings], handle, indent=2)
        self._pf_status.setText(f"Saved {path} (+ .json)")

    # -- Root Builder -------------------------------------------------------

    def _open_move_root(self) -> None:
        from .tools_ui import MoveToRootDialog
        MoveToRootDialog(self._organizer, self._log, self._offer_root_builder,
                         self._root_builder_installed, self).exec()

    def _plugins_dir(self) -> str:
        try:
            return os.path.dirname(os.path.normpath(self._organizer.pluginDataPath()))
        except Exception:
            from PyQt6.QtCore import QCoreApplication
            return os.path.join(QCoreApplication.applicationDirPath(), "plugins")

    def _root_builder_installed(self) -> bool:
        try:
            if self._organizer.isPluginEnabled(RB.PLUGIN_NAME):
                return True
        except Exception:
            pass
        return RB.is_installed(self._plugins_dir())

    def _offer_root_builder(self, reason: str = "") -> None:
        if self._root_builder_installed():
            QMessageBox.information(self, PLUGIN_DISPLAY, "Root Builder is already installed.")
            return
        box = QMessageBox(self)
        box.setWindowTitle(PLUGIN_DISPLAY)
        box.setText((reason + "\n\n" if reason else "") +
                    "Root Builder (by Kezyma) deploys the files in each mod's Root folder - SKSE "
                    "loader, ENB, preloaders - into the game folder when MO2 starts a program.\n\n"
                    f"Download the latest release from Kezyma's GitHub and install it into:\n"
                    f"{self._plugins_dir()}")
        install = box.addButton("Download && install", QMessageBox.ButtonRole.AcceptRole)
        nexus = box.addButton("Open Nexus page", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is nexus:
            QDesktopServices.openUrl(QUrl(RB.NEXUS_URL))
        elif box.clickedButton() is install:
            self._log("Root Builder: looking up the latest release...", force=True)
            self._rb_http.get(RB.RELEASE_ASSETS_URL, self._on_rb_assets)

    def _rb_failed(self, why: str) -> None:
        self._log(f"Root Builder install failed: {why}", force=True)
        if QMessageBox.question(
                self, PLUGIN_DISPLAY,
                f"Could not install Root Builder automatically:\n{why}\n\nOpen its Nexus page to "
                "install it by hand? (Extract the 'rootbuilder' folder into MO2's plugins folder.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl(RB.NEXUS_URL))

    def _on_rb_assets(self, code, text, _ctype) -> None:
        asset = RB.latest_asset(text or "")
        if code != 200 or asset is None:
            self._rb_failed(f"no release zip found (HTTP {code})")
            return
        url, version = asset
        target = os.path.join(self._data_dir(), f"rootbuilder.{version}.zip")
        self._log(f"Root Builder {version}: downloading {url}", force=True)
        self._rb_http.download(url, target, lambda got, total: None,
                               lambda ok, info: self._on_rb_zip(ok, info, version))

    def _on_rb_zip(self, ok: bool, info: str, version: str) -> None:
        if not ok:
            self._rb_failed(info)
            return
        plugins = self._plugins_dir()
        try:
            _prefix, folders = RB.plugin_folders_in_zip(info)
            existing = [f for f in folders if os.path.exists(os.path.join(plugins, f))]
            if existing and QMessageBox.question(
                    self, PLUGIN_DISPLAY,
                    "These folders already exist in MO2's plugins folder and will be overwritten:\n"
                    + "\n".join(existing) + "\n\nContinue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                return
            written = RB.install_zip(info, plugins)
        except Exception as exc:
            self._rb_failed(str(exc))
            return
        self._log(f"Root Builder {version} installed: {', '.join(written)}", force=True)
        QMessageBox.information(
            self, PLUGIN_DISPLAY,
            f"Root Builder {version} installed ({', '.join(written)}).\n\nRestart Mod Organizer 2 to "
            "load it. Its default 'Copy' mode copies Root files into the game folder on launch and "
            "removes them afterwards; settings are under Tools > Root Builder.")

    # -- shutdown ---------------------------------------------------------

    def done(self, result: int) -> None:  # noqa: D401 - Qt override
        if self._online is not None:
            self._online.stop()
        for worker in (self._url_checker, self._checker, self._downloader):
            if worker is not None:
                try:
                    worker.stop()
                except Exception:
                    pass
        self._collection_tab.stop()
        # MO2's download callbacks are permanent; detach them from this window.
        self._downloader = None
        self._collection_tab.runner = None
        super().done(result)
