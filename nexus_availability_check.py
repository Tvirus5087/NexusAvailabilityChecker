# -*- coding: utf-8 -*-
"""
Nexus Availability Check - a Mod Organizer 2 tool plugin.

Performs a "dry run download" pass over every Nexus-sourced entry MO2 knows
about (downloads/*.meta files, and optionally installed mods) and reports any
mod page or file that Nexus no longer serves - i.e. removed by the author,
hidden, or pulled by moderation. Intended for validating Wabbajack modlists
before a build/release.

Requirements
------------
* Mod Organizer 2.5.0 or newer (PyQt6 / mobase for Qt6).
* The user must have linked MO2 to Nexus ("Settings -> Nexus -> Connect to
  Nexus"). This plugin never asks for, stores, or reads an API key: all
  requests go through IOrganizer.createNexusBridge(), which reuses MO2's own
  authenticated Nexus session and its rate-limit handling.

Install
-------
Drop this file into  <Mod Organizer install>/plugins/nexus_availability_check.py
then restart MO2. It appears under the Tools (puzzle piece) menu as
"Nexus Availability Check".

Notes on the checks
-------------------
* One /files request is made per unique (game, modID). All file IDs belonging
  to that mod are then resolved locally, so a 3000-download modlist costs
  roughly one API call per unique mod page, not per archive.
* A failed mod request is re-queried with a description request to capture the
  Nexus status string (hidden / under moderation / not published / removed).
* A file ID that is absent from the mod's file list is flagged. Note that
  Nexus also drops *permanently deleted* old versions from this list, while
  merely "archived" old versions normally remain - so a flagged file means the
  exact file Wabbajack wants can no longer be fetched.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import mobase

from PyQt6.QtCore import (
    QObject,
    QSettings,
    QTimer,
    QUrl,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QPalette
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
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

try:  # MO2 routes these into its own log
    from PyQt6.QtCore import qInfo, qWarning
except ImportError:  # pragma: no cover
    def qInfo(msg: str) -> None:  # type: ignore
        print(msg)

    def qWarning(msg: str) -> None:  # type: ignore
        print(msg)


PLUGIN_NAME = "Nexus Availability Check"
PLUGIN_VERSION = (1, 0, 0)
DATA_SUBDIR = "nexus_availability_check"
CACHE_FILE = "verified_cache.json"

# ---------------------------------------------------------------------------
# Result states
# ---------------------------------------------------------------------------

ST_PENDING = "PENDING"
ST_OK = "OK"
ST_CACHED = "OK (cached)"
ST_MOD_GONE = "VERIFY NEXUS AVAILABILITY"   # mod page unreachable
ST_FILE_GONE = "VERIFY NEXUS AVAILABILITY"  # file no longer listed
ST_ERROR = "ERROR"
ST_SKIPPED = "SKIPPED"
ST_DOWNLOADING = "DOWNLOADING"
ST_DOWNLOADED = "DOWNLOADED"
ST_DL_FAILED = "DOWNLOAD FAILED"

FLAGGED_STATES = {ST_MOD_GONE, ST_FILE_GONE}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


KIND_NEXUS = "nexus"
KIND_DIRECT = "directURL"
KIND_MANUAL = "manualURL"
KIND_IPS4 = "ips4"
KIND_UNKNOWN = "unknown"


@dataclass
class Entry:
    """One thing we want to prove is still downloadable."""

    source: str                 # "download" | "mod"
    game: str                   # MO2 game name as written in the meta file, e.g. "SkyrimSE"
    mod_id: int
    file_id: Optional[int]
    kind: str = KIND_NEXUS
    url: str = ""               # directURL / manualURL for non-Nexus entries
    prompt: str = ""            # manualURL prompt, if the author supplied one
    archive: str = ""           # archive file name (downloads) or installation file (mods)
    mod_name: str = ""
    display_name: str = ""
    version: str = ""
    meta_path: str = ""
    local_size: Optional[int] = None
    remote_size: Optional[int] = None
    status: str = ST_PENDING
    reason: str = ""
    nexus_status: str = ""

    @property
    def key(self) -> Tuple[str, int]:
        return (self.game.lower(), self.mod_id)

    @property
    def cache_key(self) -> str:
        if self.kind != KIND_NEXUS:
            return f"{self.kind}|{self.url}"
        return f"{self.game.lower()}|{self.mod_id}|{self.file_id if self.file_id else 0}"

    @property
    def flagged(self) -> bool:
        return self.status in FLAGGED_STATES

    @property
    def label(self) -> str:
        if self.kind == KIND_NEXUS:
            return f"nexus{' (installed)' if self.source == 'mod' else ''}"
        return self.kind


# ---------------------------------------------------------------------------
# Scanning MO2 state
# ---------------------------------------------------------------------------


def _meta_value(settings: QSettings, *names: str) -> Optional[str]:
    """Read the first key that exists, tolerating case differences."""
    for name in names:
        for candidate in (name, f"General/{name}"):
            value = settings.value(candidate)
            if value is not None and str(value).strip() != "":
                return str(value).strip()
    return None


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip().strip('"'))
    except (TypeError, ValueError):
        return None


def scan_downloads(downloads_path: str, require_archive: bool) -> List[Entry]:
    """Parse every *.meta in the downloads folder that came from Nexus."""
    entries: List[Entry] = []
    if not downloads_path or not os.path.isdir(downloads_path):
        return entries

    for name in sorted(os.listdir(downloads_path)):
        if not name.lower().endswith(".meta"):
            continue
        meta_path = os.path.join(downloads_path, name)
        archive_path = meta_path[: -len(".meta")]
        archive_name = os.path.basename(archive_path)

        if require_archive and not os.path.isfile(archive_path):
            continue

        try:
            # QSettings copes with MO2's quoted values and @Variant blobs;
            # configparser does not.
            meta = QSettings(meta_path, QSettings.Format.IniFormat)
        except Exception as exc:  # pragma: no cover - defensive
            qWarning(f"[{PLUGIN_NAME}] could not read {meta_path}: {exc}")
            continue

        repository = (_meta_value(meta, "repository") or "").strip('"')
        game = (_meta_value(meta, "gameName") or "").strip('"')
        mod_id = _to_int(_meta_value(meta, "modID", "modid"))
        file_id = _to_int(_meta_value(meta, "fileID", "fileid"))
        direct_url = (_meta_value(meta, "directURL", "directurl") or "").strip('"')
        manual_url = (_meta_value(meta, "manualURL", "manualurl") or "").strip('"')
        ips4_site = (_meta_value(meta, "ips4Site") or "").strip('"')

        local_size: Optional[int] = None
        if os.path.isfile(archive_path):
            try:
                local_size = os.path.getsize(archive_path)
            except OSError:
                local_size = None

        common = dict(
            source="download",
            archive=archive_name,
            mod_name=(_meta_value(meta, "modName") or "").strip('"'),
            display_name=(_meta_value(meta, "name") or "").strip('"'),
            version=(_meta_value(meta, "version") or "").strip('"'),
            meta_path=meta_path,
            local_size=local_size,
        )

        is_nexus = bool(game and mod_id and mod_id > 0) and (
            not repository or repository.lower() == "nexus"
        )

        if is_nexus:
            entries.append(
                Entry(
                    game=game,
                    mod_id=int(mod_id),
                    file_id=file_id if (file_id and file_id > 0) else None,
                    kind=KIND_NEXUS,
                    **common,
                )
            )
        elif direct_url:
            entries.append(
                Entry(
                    game=game,
                    mod_id=0,
                    file_id=None,
                    kind=KIND_DIRECT,
                    url=direct_url,
                    **common,
                )
            )
        elif manual_url:
            entries.append(
                Entry(
                    game=game,
                    mod_id=0,
                    file_id=None,
                    kind=KIND_MANUAL,
                    url=manual_url,
                    prompt=(_meta_value(meta, "prompt") or "").strip('"'),
                    **common,
                )
            )
        elif ips4_site:
            ips4_mod = _meta_value(meta, "ips4Mod") or ""
            ips4_file = (_meta_value(meta, "ips4File") or "").strip('"')
            entry = Entry(
                game=game,
                mod_id=0,
                file_id=None,
                kind=KIND_IPS4,
                url="",
                **common,
            )
            entry.status = ST_SKIPPED
            entry.reason = (
                f"ips4 meta ({ips4_site} / {ips4_mod} / {ips4_file}) - Wabbajack has "
                "deprecated this source type; convert it to manualURL"
            )
            entries.append(entry)

    return entries


def installed_file_ids(mod_path: str) -> List[Tuple[int, int]]:
    """Read (modID, fileID) pairs from an installed mod's meta.ini.

    MO2 records what was installed into a mod under [installedFiles], e.g.

        [installedFiles]
        1\\modid=598
        1\\fileid=678881
        size=1

    This is the only place the file ID survives once the download and its
    .meta have been deleted, so it is what makes re-downloading possible.
    """
    meta_ini = os.path.join(mod_path, "meta.ini")
    if not os.path.isfile(meta_ini):
        return []

    mod_ids: Dict[str, int] = {}
    file_ids: Dict[str, int] = {}
    try:
        with open(meta_ini, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return []

    section = re.search(
        r"^\[installedFiles\]\s*$(.*?)(?=^\[|\Z)", raw, re.MULTILINE | re.DOTALL
    )
    if section:
        body = section.group(1)
        for index, value in re.findall(
            r"^\s*(\d+)\\+modid\s*=\s*(\d+)", body, re.MULTILINE | re.IGNORECASE
        ):
            mod_ids[index] = int(value)
        for index, value in re.findall(
            r"^\s*(\d+)\\+fileid\s*=\s*(\d+)", body, re.MULTILINE | re.IGNORECASE
        ):
            file_ids[index] = int(value)

    pairs: List[Tuple[int, int]] = []
    for index, file_id in sorted(file_ids.items(), key=lambda kv: int(kv[0])):
        mod_id = mod_ids.get(index, 0)
        if file_id > 0:
            pairs.append((mod_id, file_id))
    return pairs


def scan_installed_mods(organizer: mobase.IOrganizer) -> List[Entry]:
    """Installed mods that carry a Nexus ID, for archives no longer on disk."""
    entries: List[Entry] = []
    mod_list = organizer.modList()
    for mod_name in mod_list.allMods():
        mod = mod_list.getMod(mod_name)
        if mod is None:
            continue
        try:
            mod_id = int(mod.nexusId())
        except Exception:
            continue
        if mod_id <= 0:
            continue

        game = ""
        try:
            game = mod.gameName() or ""
        except Exception:
            game = ""
        if not game:
            managed = organizer.managedGame()
            game = managed.gameName() if managed else ""
        if not game:
            continue

        archive = ""
        try:
            archive = mod.installationFile() or ""
        except Exception:
            archive = ""

        version = ""
        try:
            version = mod.version().displayString()
        except Exception:
            version = ""

        # Recover the file ID from meta.ini so the archive can be re-downloaded
        # even though its download entry (and .meta) are long gone.
        file_id: Optional[int] = None
        try:
            for candidate_mod, candidate_file in installed_file_ids(mod.absolutePath()):
                if candidate_mod in (0, mod_id):
                    file_id = candidate_file
                    break
        except Exception:
            file_id = None

        entries.append(
            Entry(
                source="mod",
                game=game,
                mod_id=mod_id,
                file_id=file_id,
                archive=os.path.basename(archive),
                mod_name=mod_name,
                display_name=mod_name,
                version=version,
            )
        )
    return entries


def dedupe(entries: Iterable[Entry]) -> List[Entry]:
    entries = list(entries)
    # A download meta carries the file ID, so prefer it over the installed-mod
    # view of the same Nexus mod instead of listing both.
    from_downloads = {
        (e.game.lower(), e.mod_id)
        for e in entries
        if e.source == "download" and e.kind == KIND_NEXUS and e.mod_id
    }
    seen: Set[Tuple[Any, ...]] = set()
    out: List[Entry] = []
    for entry in entries:
        if (
            entry.source == "mod"
            and entry.kind == KIND_NEXUS
            and (entry.game.lower(), entry.mod_id) in from_downloads
        ):
            continue
        marker = (entry.kind, entry.game.lower(), entry.mod_id, entry.file_id, entry.url, entry.source)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Nexus querying, via MO2's own authenticated bridge
# ---------------------------------------------------------------------------


def _split_signal_args(args: Sequence[Any]) -> Tuple[List[str], List[int], List[Any]]:
    """The bridge signal signatures differ slightly between MO2 builds, so
    connect with *args and classify by type instead of trusting positions."""
    strings = [a for a in args if isinstance(a, str)]
    ints = [a for a in args if isinstance(a, int) and not isinstance(a, bool)]
    others = [a for a in args if not isinstance(a, (str, int, bool))]
    return strings, ints, others


_NOT_FOUND_PATTERNS = (
    "404",
    "not found",
    "no such",
    "does not exist",
)
_FORBIDDEN_PATTERNS = ("403", "forbidden", "unauthor")
_RATE_LIMIT_PATTERNS = ("429", "rate limit", "too many requests")


def _classify_error(message: str) -> Tuple[str, str]:
    """Return (status, human reason) for a failed request."""
    lowered = (message or "").lower()
    if any(p in lowered for p in _RATE_LIMIT_PATTERNS):
        return ST_ERROR, f"Nexus rate limit hit ({message})"
    if any(p in lowered for p in _NOT_FOUND_PATTERNS):
        return ST_MOD_GONE, "Mod page returns not-found - deleted or hidden"
    if any(p in lowered for p in _FORBIDDEN_PATTERNS):
        return ST_MOD_GONE, f"Access refused by Nexus - moderated or hidden ({message})"
    return ST_ERROR, message or "request failed"


class AvailabilityChecker(QObject):
    """Drives the Nexus bridge over a queue of requests.

    Important: MO2's bridge exposes filesAvailable as
    QList<ModRepositoryFileInfo*>, which PyQt cannot marshal - touching that
    attribute raises TypeError, so requestFiles() is unusable from Python.
    Everything here therefore goes through requestDescription() (mod page
    status) and requestFileInfo() (single file), both of which deliver their
    payload as a QVariant that converts cleanly to a dict.

    Phase 1: one requestDescription per unique (game, modID).
    Phase 2: one requestFileInfo per entry with a file ID, but only for mods
             that survived phase 1 - no point asking about files on a mod page
             that is already gone.
    """

    progressed = pyqtSignal(int, int)
    groupResolved = pyqtSignal(object)   # list of entry indices touched
    logged = pyqtSignal(str)
    debug = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        organizer: mobase.IOrganizer,
        entries: List[Entry],
        max_in_flight: int = 4,
        timeout_seconds: int = 45,
        query_status_on_failure: bool = True,
        verify_file_ids: bool = True,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._organizer = organizer
        self._entries = entries
        self._max_in_flight = max(1, max_in_flight)
        self._timeout = max(10, timeout_seconds)
        self._verify_files = verify_file_ids
        self._stopping = False
        self._running = False
        self._user_data: Any = None
        self._user_data_known = False

        # (game_lower, mod_id) -> entry indices
        self._mod_groups: Dict[Tuple[str, int], List[int]] = {}
        # (game_lower, mod_id, file_id) -> entry indices
        self._file_groups: Dict[Tuple[str, int, int], List[int]] = {}
        self._game_of: Dict[Tuple[str, int], str] = {}

        for index, entry in enumerate(entries):
            if entry.status in (ST_CACHED, ST_SKIPPED):
                continue
            if entry.kind != KIND_NEXUS:
                continue
            mod_key = entry.key
            self._mod_groups.setdefault(mod_key, []).append(index)
            self._game_of.setdefault(mod_key, entry.game)
            if entry.file_id:
                file_key = (mod_key[0], mod_key[1], entry.file_id)
                self._file_groups.setdefault(file_key, []).append(index)

        self._bridge = organizer.createNexusBridge()
        if self._bridge is None:
            raise RuntimeError(
                "createNexusBridge() returned None - MO2 has no Nexus interface available."
            )

        self._connected: List[str] = []
        self._sig_description = self._connect_signal("descriptionAvailable", self._on_description)
        self._sig_file_info = self._connect_signal("fileInfoAvailable", self._on_file_info)
        self._sig_failed = self._connect_signal("requestFailed", self._on_failed)

        if self._sig_description is None:
            raise RuntimeError(
                "The Nexus bridge exposes no usable 'descriptionAvailable' signal on this "
                "MO2 build, so nothing can be checked."
            )
        if self._sig_failed is None:
            raise RuntimeError(
                "The Nexus bridge exposes no usable 'requestFailed' signal on this MO2 "
                "build - removed mods could not be told apart from slow ones."
            )
        if self._verify_files and self._sig_file_info is None:
            self._verify_files = False
            self.logged.emit(
                "This MO2 build cannot deliver per-file results to Python; falling back "
                "to mod-page checks only."
            )

        self._queue: List[Tuple[str, int, Optional[int]]] = [
            (key[0], key[1], None) for key in self._mod_groups
        ]
        self._inflight: Dict[Tuple[str, int, Optional[int]], float] = {}
        self._phase = "description"
        self._mod_ok: Set[Tuple[str, int]] = set()
        self._done = 0
        self._total = len(self._queue) + (len(self._file_groups) if self._verify_files else 0)

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(3000)
        self._watchdog.timeout.connect(self._check_timeouts)

    def _connect_signal(self, name: str, handler) -> Optional[str]:
        """Bind a bridge signal, tolerating ones PyQt refuses to marshal."""
        try:
            signal = getattr(self._bridge, name)
        except TypeError as exc:
            # e.g. filesAvailable -> QList<ModRepositoryFileInfo*>
            self.debug.emit(f"!! signal '{name}' unusable from Python: {exc}")
            return None
        except AttributeError:
            self.debug.emit(f"!! bridge has no signal '{name}'")
            return None
        try:
            signal.connect(handler)
        except Exception as exc:
            self.debug.emit(f"!! could not connect '{name}': {exc}")
            return None
        self._connected.append(name)
        return name

    # -- control -----------------------------------------------------------

    @property
    def total_groups(self) -> int:
        return self._total

    @property
    def verifying_files(self) -> bool:
        return self._verify_files

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        if not self._queue:
            self._finish()
            return
        self._watchdog.start()
        self._pump()

    def stop(self) -> None:
        self._stopping = True
        self._queue.clear()
        if not self._inflight:
            self._finish()

    # -- queue -------------------------------------------------------------

    def _send(self, key: Tuple[str, int, Optional[int]], game: str) -> Tuple[bool, str]:
        """Issue one request, working out which user_data type the bindings accept."""
        candidates = [self._user_data] if self._user_data_known else [None, "", 0, []]
        errors: List[str] = []
        for user_data in candidates:
            try:
                if key[2] is None:
                    self._bridge.requestDescription(game, key[1], user_data)
                else:
                    self._bridge.requestFileInfo(game, key[1], key[2], user_data)
                if not self._user_data_known:
                    self._user_data = user_data
                    self._user_data_known = True
                    self.debug.emit(f"   bindings accept user_data={user_data!r}")
                return True, ""
            except (TypeError, ValueError) as exc:
                errors.append(f"user_data={user_data!r}: {exc}")
            except Exception as exc:  # pragma: no cover - defensive
                return False, str(exc)
        return False, " | ".join(errors) or "unknown error"

    def _pump(self) -> None:
        while (
            not self._stopping
            and self._queue
            and len(self._inflight) < self._max_in_flight
        ):
            key = self._queue.pop(0)
            game = self._game_of.get((key[0], key[1]), key[0])
            self._inflight[key] = time.monotonic()
            label = "description" if key[2] is None else f"fileInfo {key[2]}"
            self.debug.emit(f"-> {label}: game={game!r} modID={key[1]}")
            sent, error = self._send(key, game)
            if not sent:
                self._inflight.pop(key, None)
                self.debug.emit(f"!! bridge call rejected: {error}")
                self._resolve(key, ST_ERROR, f"bridge call failed: {error}")

        if not self._queue and not self._inflight:
            if self._phase == "description" and not self._stopping and self._verify_files:
                self._begin_file_phase()
            else:
                self._finish()

    def _begin_file_phase(self) -> None:
        self._phase = "file"
        queue = [key for key in self._file_groups if (key[0], key[1]) in self._mod_ok]
        skipped = len(self._file_groups) - len(queue)
        if skipped:
            # Their mod page is already flagged; don't spend API calls on them.
            self._done += skipped
            self.progressed.emit(self._done, self._total)
        if not queue:
            self._finish()
            return
        self._queue = queue
        self.logged.emit(
            f"Mod pages done. Verifying {len(queue)} individual file ID(s)."
        )
        self._pump()

    def _finish(self) -> None:
        self._watchdog.stop()
        self._running = False
        self.finished.emit()

    def _check_timeouts(self) -> None:
        now = time.monotonic()
        for key, started in list(self._inflight.items()):
            if now - started >= self._timeout:
                self._inflight.pop(key, None)
                self._resolve(
                    key, ST_ERROR, f"no response from Nexus within {self._timeout}s"
                )
        self._pump()

    # -- resolution --------------------------------------------------------

    def _indices_for(self, key: Tuple[str, int, Optional[int]]) -> List[int]:
        if key[2] is None:
            return self._mod_groups.get((key[0], key[1]), [])
        return self._file_groups.get((key[0], key[1], key[2]), [])

    def _resolve(
        self,
        key: Tuple[str, int, Optional[int]],
        status: str,
        reason: str,
        nexus_status: str = "",
        pending_files: bool = False,
    ) -> None:
        indices = self._indices_for(key)
        for index in indices:
            entry = self._entries[index]
            if nexus_status:
                entry.nexus_status = nexus_status
            if pending_files and entry.file_id:
                # Mod page is fine; the file itself is checked in phase 2.
                entry.reason = reason
                continue
            entry.status = status
            entry.reason = reason

        self._done += 1
        self.progressed.emit(self._done, self._total)
        if indices:
            self.groupResolved.emit(indices)

    # -- bridge signals ----------------------------------------------------

    def _dump(self, label: str, args: Sequence[Any]) -> None:
        parts = []
        for arg in args:
            text = repr(arg)
            if len(text) > 140:
                text = text[:140] + "..."
            parts.append(f"{type(arg).__name__}={text}")
        self.debug.emit(f"<- {label}({len(args)} args): " + ", ".join(parts))

    def _match(
        self, strings: List[str], ints: List[int]
    ) -> Optional[Tuple[str, int, Optional[int]]]:
        """Signal argument order varies between MO2 builds, so match against
        what is actually in flight rather than trusting positions."""
        if not ints:
            return None
        values = set(ints)
        games = {s.lower() for s in strings}
        fallback: Optional[Tuple[str, int, Optional[int]]] = None
        for key in self._inflight:
            if key[1] not in values:
                continue
            if key[2] is not None:
                if key[2] in values:
                    return key
                continue
            if not games or key[0] in games:
                return key
            fallback = fallback or key
        return fallback

    @staticmethod
    def _payload(args: Sequence[Any]) -> Optional[dict]:
        for arg in args:
            if isinstance(arg, dict):
                return arg
        return None

    def _on_description(self, *args: Any) -> None:
        self._dump("descriptionAvailable", args)
        strings, ints, _ = _split_signal_args(args)
        key = self._match(strings, ints)
        if key is None or key not in self._inflight:
            self.debug.emit(f"   ignored (no in-flight match for {key!r})")
            return
        self._inflight.pop(key, None)
        mod_key = (key[0], key[1])

        payload = self._payload(args)
        status_text = ""
        available: Optional[bool] = None
        if payload is not None:
            status_text = str(payload.get("status", "") or "")
            if "available" in payload:
                available = bool(payload.get("available"))

        unavailable = available is False or (
            status_text and status_text.lower() not in ("published", "public")
        )

        if unavailable:
            self._resolve(
                key,
                ST_MOD_GONE,
                f"Nexus reports the mod as '{status_text or 'unavailable'}'",
                nexus_status=status_text,
            )
        else:
            self._mod_ok.add(mod_key)
            if self._verify_files:
                self._resolve(
                    key,
                    ST_OK,
                    "Mod page available; file check queued",
                    nexus_status=status_text,
                    pending_files=True,
                )
            else:
                self._resolve(
                    key, ST_OK, "Mod page available", nexus_status=status_text
                )
        self._pump()

    def _on_file_info(self, *args: Any) -> None:
        self._dump("fileInfoAvailable", args)
        strings, ints, _ = _split_signal_args(args)
        key = self._match(strings, ints)
        if key is None or key not in self._inflight:
            self.debug.emit(f"   ignored (no in-flight match for {key!r})")
            return
        self._inflight.pop(key, None)

        payload = self._payload(args)
        category = ""
        if payload is not None:
            category = str(payload.get("category_name", "") or "")

        if payload is not None and payload.get("category_name", "x") is None:
            # Nexus nulls category_name on files pulled from public view.
            self._resolve(
                key,
                ST_FILE_GONE,
                "File exists but Nexus no longer serves it (category cleared)",
            )
        else:
            suffix = f" ({category})" if category else ""
            self._resolve(key, ST_OK, f"Mod page and file both available{suffix}")
        self._pump()

    def _on_failed(self, *args: Any) -> None:
        self._dump("requestFailed", args)
        strings, ints, _ = _split_signal_args(args)
        key = self._match(strings, ints)
        if key is None or key not in self._inflight:
            self.debug.emit(f"   ignored (no in-flight match for {key!r})")
            return
        self._inflight.pop(key, None)

        message = strings[-1] if len(strings) > 1 else (strings[0] if strings else "")
        lowered = (message or "").lower()

        if any(p in lowered for p in _RATE_LIMIT_PATTERNS):
            self._resolve(key, ST_ERROR, f"Nexus rate limit hit ({message})")
            self.logged.emit(
                "Nexus rate limit reached - stopping. Entries already confirmed OK are "
                "cached, so a later run picks up where this one left off."
            )
            self.stop()
            return

        status, reason = _classify_error(message)
        if key[2] is not None and status == ST_MOD_GONE:
            reason = f"File {key[2]} is no longer available on Nexus ({message})"
        self._resolve(key, status, reason)
        self._pump()


# ---------------------------------------------------------------------------
# Non-Nexus link checking (directURL / manualURL)
# ---------------------------------------------------------------------------

WHITELIST_URL = (
    "https://raw.githubusercontent.com/wabbajack-tools/opt-out-lists/master/"
    "ServerWhitelist.yml"
)

# Hosts that answer every request with a single-page app, so an HTTP 200 says
# nothing at all about whether the file behind the link still exists.
OPAQUE_HOSTS = {
    "mega.nz": "MEGA serves the same page for live and deleted files",
    "mega.co.nz": "MEGA serves the same page for live and deleted files",
}

# Hosts where a 404 is meaningful but a 200 only proves the landing page loaded.
SOFT_HOSTS = {
    "drive.google.com": "Google Drive returns a landing/quota page rather than the file",
    "docs.google.com": "Google Drive returns a landing/quota page rather than the file",
    "www.mediafire.com": "MediaFire returns a landing page rather than the file",
    "mediafire.com": "MediaFire returns a landing page rather than the file",
    "www.dropbox.com": "Dropbox returns a landing page unless the link ends in dl=1",
    "dropbox.com": "Dropbox returns a landing page unless the link ends in dl=1",
    "disk.yandex.com": "Yandex returns a landing page rather than the file",
    "disk.yandex.ru": "Yandex returns a landing page rather than the file",
}


def _host_of(url: str) -> str:
    match = re.match(r"^\s*https?://([^/:?#]+)", url or "", re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _human_size(value: Optional[int]) -> str:
    if not value or value < 0:
        return "?"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


class UrlChecker(QObject):
    """Validates directURL / manualURL entries with plain HTTP.

    HEAD first; hosts that reject HEAD get a ranged GET for the first byte so
    we never pull a whole archive. Redirects are followed. Where the local
    archive is present its size is compared against Content-Length, which is
    the strongest signal available short of downloading.
    """

    progressed = pyqtSignal(int, int)
    entryResolved = pyqtSignal(object)   # list of entry indices
    logged = pyqtSignal(str)
    debug = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        entries: List[Entry],
        indices: List[int],
        max_in_flight: int = 4,
        timeout_seconds: int = 30,
        check_whitelist: bool = True,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        from PyQt6.QtNetwork import QNetworkAccessManager  # local: keeps import failures contained

        self._entries = entries
        self._queue = list(indices)
        self._total = len(indices)
        self._done = 0
        self._max_in_flight = max(1, max_in_flight)
        self._timeout = max(5, timeout_seconds)
        self._check_whitelist = check_whitelist
        self._stopping = False

        self._manager = QNetworkAccessManager(self)
        self._inflight: Dict[Any, Tuple[int, bool]] = {}   # reply -> (entry index, is_get)
        self._whitelist: Optional[List[str]] = None
        self._whitelist_reply: Any = None

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(2000)
        self._watchdog.timeout.connect(self._pump)

    @property
    def total(self) -> int:
        return self._total

    # -- control -----------------------------------------------------------

    def start(self) -> None:
        if not self._queue:
            self.finished.emit()
            return
        if self._check_whitelist:
            self._fetch_whitelist()
        else:
            self._begin()

    def stop(self) -> None:
        self._stopping = True
        self._queue.clear()
        for reply in list(self._inflight):
            try:
                reply.abort()
            except Exception:
                pass
        if not self._inflight:
            self._finish()

    def _begin(self) -> None:
        self._watchdog.start()
        self._pump()

    def _finish(self) -> None:
        self._watchdog.stop()
        self.finished.emit()

    # -- Wabbajack whitelist ----------------------------------------------

    def _fetch_whitelist(self) -> None:
        from PyQt6.QtCore import QUrl as _QUrl
        from PyQt6.QtNetwork import QNetworkRequest

        request = QNetworkRequest(_QUrl(WHITELIST_URL))
        request.setHeader(
            QNetworkRequest.KnownHeaders.UserAgentHeader, "MO2-NexusAvailabilityCheck/1.1"
        )
        self.debug.emit("-> fetching Wabbajack ServerWhitelist.yml")
        reply = self._manager.get(request)
        self._whitelist_reply = reply
        reply.finished.connect(lambda: self._on_whitelist(reply))

    def _on_whitelist(self, reply) -> None:
        try:
            raw = bytes(reply.readAll()).decode("utf-8", "replace")
        except Exception:
            raw = ""
        finally:
            reply.deleteLater()
            self._whitelist_reply = None

        prefixes = re.findall(r"^\s*-\s*(https?://\S+)\s*$", raw, re.MULTILINE)
        if prefixes:
            self._whitelist = [p.rstrip("/").lower() for p in prefixes]
            self.logged.emit(
                f"Loaded {len(self._whitelist)} whitelisted URL prefixes from Wabbajack."
            )
        else:
            self._whitelist = None
            self.logged.emit(
                "Could not read the Wabbajack whitelist; skipping the whitelist check."
            )
        self._begin()

    def _whitelist_note(self, url: str) -> str:
        if not self._whitelist:
            return ""
        lowered = url.lower()
        if any(lowered.startswith(prefix) for prefix in self._whitelist):
            return ""
        host = _host_of(url)
        if host.endswith(("nexusmods.com", "github.com", "wabbajack.org")):
            return ""
        return " NOT on the Wabbajack server whitelist - compilation will reject it"

    # -- requests ----------------------------------------------------------

    def _pump(self) -> None:
        while (
            not self._stopping
            and self._queue
            and len(self._inflight) < self._max_in_flight
        ):
            index = self._queue.pop(0)
            self._send(index, use_get=False)
        if not self._queue and not self._inflight and not self._stopping:
            self._finish()

    def _send(self, index: int, use_get: bool) -> None:
        from PyQt6.QtCore import QUrl as _QUrl
        from PyQt6.QtNetwork import QNetworkRequest

        entry = self._entries[index]
        host = _host_of(entry.url)
        if host in OPAQUE_HOSTS and not use_get:
            # No point spending a request: these answer identically whether the
            # file is live or deleted, and they stall on HEAD until timeout.
            self._resolve(
                index,
                ST_ERROR,
                f"Not checkable automatically - {OPAQUE_HOSTS[host]}. Open the link "
                f"and confirm by hand{self._whitelist_note(entry.url)}",
            )
            return

        url = _QUrl(entry.url)
        if not url.isValid() or not entry.url.lower().startswith(("http://", "https://")):
            self._resolve(index, ST_MOD_GONE, f"'{entry.url}' is not a usable http(s) URL")
            return

        request = QNetworkRequest(url)
        request.setHeader(
            QNetworkRequest.KnownHeaders.UserAgentHeader,
            "Mozilla/5.0 (compatible; MO2-NexusAvailabilityCheck/1.1)",
        )
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        request.setTransferTimeout(self._timeout * 1000)
        if use_get:
            request.setRawHeader(b"Range", b"bytes=0-0")

        self.debug.emit(f"-> {'GET' if use_get else 'HEAD'} {entry.url}")
        reply = self._manager.get(request) if use_get else self._manager.head(request)
        self._inflight[reply] = (index, use_get)
        reply.finished.connect(lambda: self._on_reply(reply))

    def _on_reply(self, reply) -> None:
        from PyQt6.QtNetwork import QNetworkRequest

        record = self._inflight.pop(reply, None)
        if record is None:
            reply.deleteLater()
            return
        index, was_get = record
        entry = self._entries[index]

        code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        content_type = str(
            reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader) or ""
        )
        length = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
        content_range = bytes(reply.rawHeader(b"Content-Range") or b"").decode(
            "ascii", "replace"
        )
        reply.deleteLater()

        self.debug.emit(
            f"<- {code} {entry.url} type={content_type!r} len={length!r} "
            f"range={content_range!r} err={error}"
        )

        # Some servers reject HEAD outright; retry those once with a ranged GET.
        if not was_get and code in (400, 403, 405, 501):
            self.debug.emit("   HEAD refused, retrying with ranged GET")
            self._send(index, use_get=True)
            return

        self._classify(index, code, content_type, length, content_range)
        self._pump()

    def _classify(
        self,
        index: int,
        code: Optional[int],
        content_type: str,
        length: Any,
        content_range: str,
    ) -> None:
        entry = self._entries[index]
        host = _host_of(entry.url)
        note = self._whitelist_note(entry.url)

        remote_size: Optional[int] = None
        if content_range:
            match = re.search(r"/(\d+)\s*$", content_range)
            if match:
                remote_size = int(match.group(1))
        elif isinstance(length, int) and length > 1:
            remote_size = length
        entry.remote_size = remote_size

        if code is None:
            self._resolve(index, ST_ERROR, f"no HTTP response (host unreachable){note}")
            return

        if code in (404, 410):
            self._resolve(index, ST_MOD_GONE, f"HTTP {code} - the file is gone{note}")
            return
        if code in (401, 403):
            self._resolve(
                index,
                ST_ERROR,
                f"HTTP {code} - the host refused an anonymous check; verify by hand{note}",
            )
            return
        if code == 429:
            self._resolve(index, ST_ERROR, f"HTTP 429 - rate limited by the host{note}")
            return
        if code >= 500:
            self._resolve(index, ST_ERROR, f"HTTP {code} - host error, try again later{note}")
            return
        if code >= 400:
            self._resolve(index, ST_MOD_GONE, f"HTTP {code}{note}")
            return

        # 2xx / 3xx from here on.
        if host in OPAQUE_HOSTS:
            self._resolve(
                index,
                ST_ERROR,
                f"Link responds, but {OPAQUE_HOSTS[host]} - check this one by hand{note}",
            )
            return

        looks_like_page = content_type.lower().startswith(("text/html", "text/plain"))

        if entry.kind == KIND_MANUAL:
            self._resolve(
                index,
                ST_OK,
                f"Landing page reachable (HTTP {code}). manualURL contents cannot be "
                f"verified automatically - confirm the file is still on the page{note}",
            )
            return

        if host in SOFT_HOSTS:
            self._resolve(
                index,
                ST_OK,
                f"Reachable (HTTP {code}), but {SOFT_HOSTS[host]}{note}",
            )
            return

        if looks_like_page:
            self._resolve(
                index,
                ST_FILE_GONE,
                f"HTTP {code} but the server returned an HTML page instead of a file - "
                f"usually a 'not found' page with a 200 status{note}",
            )
            return

        if (
            entry.local_size
            and remote_size
            and abs(remote_size - entry.local_size) > 0
        ):
            self._resolve(
                index,
                ST_FILE_GONE,
                f"File is served but its size changed: {_human_size(remote_size)} online "
                f"vs {_human_size(entry.local_size)} locally - the author likely "
                f"re-uploaded different content{note}",
            )
            return

        size_text = f", {_human_size(remote_size)}" if remote_size else ""
        if note:
            self._resolve(index, ST_FILE_GONE, f"Downloadable (HTTP {code}{size_text}) but{note}")
        else:
            self._resolve(index, ST_OK, f"Downloadable (HTTP {code}{size_text})")

    def _resolve(self, index: int, status: str, reason: str) -> None:
        entry = self._entries[index]
        entry.status = status
        entry.reason = reason
        self._done += 1
        self.progressed.emit(self._done, self._total)
        self.entryResolved.emit([index])


# ---------------------------------------------------------------------------
# Fetching missing archives through MO2's own download manager
# ---------------------------------------------------------------------------


def manual_download_url(organizer: mobase.IOrganizer, entry: Entry) -> str:
    """A link the user can click to fetch this by hand.

    For Nexus entries with a known file ID this builds the mod-manager download
    link (nmm=1), which hands MO2 an nxm:// link through the browser. That path
    works without Premium, unlike the download API.
    """
    if entry.kind != KIND_NEXUS:
        return entry.url
    domain = nexus_domain(organizer, entry.game)
    base = f"https://www.nexusmods.com/{domain}/mods/{entry.mod_id}"
    if entry.file_id:
        return f"{base}?tab=files&file_id={entry.file_id}&nmm=1"
    return f"{base}?tab=files"


def archive_missing(entry: Entry, downloads_path: str) -> bool:
    if entry.archive:
        return not os.path.isfile(os.path.join(downloads_path, entry.archive))
    # An installed-mod entry only survives deduplication when no download meta
    # references it, so there is nothing in the downloads folder for it.
    return entry.source == "mod"


def managed_game_identifiers(organizer: mobase.IOrganizer) -> Tuple[Set[str], str]:
    """Every name the managed game answers to, lowercased, plus a display name.

    A meta file records gameName=SkyrimSE, but IPluginGame.gameName() returns
    "Skyrim Special Edition" - the short name lives on gameShortName() and the
    Nexus domain on gameNexusName(). Comparing against only one of those never
    matches.
    """
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
            values = getattr(game, accessor)() or []
        except Exception:
            continue
        for value in values:
            if value:
                names.add(str(value).lower())

    return names, display


def download_blocker(
    entry: Entry,
    managed_names: Set[str],
    managed_display: str = "",
    allow_unverified: bool = False,
) -> str:
    """Why this entry cannot be fetched automatically, or '' if it can."""
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
            return (
                f"belongs to {entry.game}; MO2 can only auto-download for the managed "
                f"game ({managed_display or 'current game'})"
            )
        return ""
    return "unknown source type"


class DownloadRunner(QObject):
    """Queues missing archives through IDownloadManager.

    Nexus files go via startDownloadNexusFile(), which uses MO2's linked
    account. directURL entries go via startDownloadURLs(). Both return a
    download ID, so completion callbacks can be matched back to the entry that
    asked for them rather than guessing.
    """

    progressed = pyqtSignal(int, int)
    entryResolved = pyqtSignal(object)
    logged = pyqtSignal(str)
    debug = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        organizer: mobase.IOrganizer,
        entries: List[Entry],
        indices: List[int],
        max_active: int = 2,
        restore_meta: bool = True,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._organizer = organizer
        self._entries = entries
        self._queue = list(indices)
        self._total = len(indices)
        self._done = 0
        self._max_active = max(1, max_active)
        self._restore_meta = restore_meta
        self._stopping = False
        self._downloads_path = organizer.downloadsPath()

        self._manager = organizer.downloadManager()
        if self._manager is None:
            raise RuntimeError("MO2 provided no download manager.")

        self._active: Dict[int, int] = {}      # download ID -> entry index
        self._pending_ids: Set[int] = set()

        self._poll = QTimer(self)
        self._poll.setInterval(4000)
        self._poll.timeout.connect(self._poll_disk)

    @property
    def total(self) -> int:
        return self._total

    # -- control -----------------------------------------------------------

    def start(self) -> None:
        if not self._queue:
            self.finished.emit()
            return
        self._poll.start()
        self._pump()

    def stop(self) -> None:
        """Stops queueing. Downloads already handed to MO2 keep running - they
        are MO2's now, and cancelling them belongs in its Downloads tab."""
        self._stopping = True
        remaining = len(self._queue)
        self._queue.clear()
        if remaining:
            self.logged.emit(
                f"Stopped. {remaining} archive(s) were not queued; downloads already "
                "handed to MO2 continue in its Downloads tab."
            )
        if not self._active:
            self._finish()

    def _finish(self) -> None:
        self._poll.stop()
        self.finished.emit()

    # -- queueing ----------------------------------------------------------

    def _pump(self) -> None:
        while not self._stopping and self._queue and len(self._active) < self._max_active:
            index = self._queue.pop(0)
            self._launch(index)
        if not self._queue and not self._active:
            self._finish()

    def _launch(self, index: int) -> None:
        entry = self._entries[index]
        try:
            if entry.kind == KIND_NEXUS:
                self.debug.emit(
                    f"-> startDownloadNexusFile(mod={entry.mod_id}, file={entry.file_id})"
                )
                download_id = self._manager.startDownloadNexusFile(
                    int(entry.mod_id), int(entry.file_id)
                )
            else:
                self.debug.emit(f"-> startDownloadURLs([{entry.url}])")
                download_id = self._manager.startDownloadURLs([entry.url])
        except Exception as exc:
            self._settle(index, ST_DL_FAILED, f"MO2 refused the download: {exc}")
            return

        if download_id is None or int(download_id) < 0:
            self._settle(
                index,
                ST_DL_FAILED,
                "MO2 returned no download ID. For Nexus files this usually means the "
                "account is not Premium - the download API is Premium-only.",
            )
            return

        download_id = int(download_id)
        self._active[download_id] = index
        entry.status = ST_DOWNLOADING
        entry.reason = f"Queued in MO2 (download #{download_id})"
        self.entryResolved.emit([index])

    # -- completion --------------------------------------------------------

    def handle_complete(self, download_id: int) -> None:
        index = self._active.pop(int(download_id), None)
        if index is None:
            return
        entry = self._entries[index]
        path = ""
        try:
            path = self._manager.downloadPath(int(download_id)) or ""
        except Exception:
            path = ""
        if path:
            entry.archive = os.path.basename(path)
        self._restore_meta_keys(entry, path)
        size = ""
        try:
            if path and os.path.isfile(path):
                size = f" ({_human_size(os.path.getsize(path))})"
        except OSError:
            size = ""
        self._settle(index, ST_DOWNLOADED, f"Downloaded{size}", already_removed=True)
        self._pump()

    def handle_failed(self, download_id: int) -> None:
        index = self._active.pop(int(download_id), None)
        if index is None:
            return
        self._settle(
            index,
            ST_DL_FAILED,
            "MO2 reported the download as failed - check its Downloads tab for the reason",
            already_removed=True,
        )
        self._pump()

    def _poll_disk(self) -> None:
        """Backstop in case a completion callback is missed."""
        for download_id, index in list(self._active.items()):
            entry = self._entries[index]
            if entry.archive and not archive_missing(entry, self._downloads_path):
                self._active.pop(download_id, None)
                self._settle(
                    index, ST_DOWNLOADED, "Downloaded (seen on disk)", already_removed=True
                )
        self._pump()

    def _settle(
        self, index: int, status: str, reason: str, already_removed: bool = False
    ) -> None:
        entry = self._entries[index]
        entry.status = status
        entry.reason = reason
        if not already_removed:
            for did, idx in list(self._active.items()):
                if idx == index:
                    self._active.pop(did, None)
        self._done += 1
        self.progressed.emit(self._done, self._total)
        self.entryResolved.emit([index])

    def _restore_meta_keys(self, entry: Entry, path: str) -> None:
        """MO2 writes its own .meta for a fresh download and does not know about
        directURL, so put it back or Wabbajack loses the source."""
        if not self._restore_meta or entry.kind not in (KIND_DIRECT, KIND_MANUAL):
            return
        if not entry.url:
            return
        meta_path = (path + ".meta") if path else ""
        if not meta_path or not os.path.isdir(os.path.dirname(meta_path)):
            return
        try:
            meta = QSettings(meta_path, QSettings.Format.IniFormat)
            key = "General/directURL" if entry.kind == KIND_DIRECT else "General/manualURL"
            if not meta.value(key):
                meta.setValue(key, entry.url)
                if entry.kind == KIND_MANUAL and entry.prompt:
                    meta.setValue("General/prompt", entry.prompt)
                meta.sync()
                self.debug.emit(f"   restored {key} in {os.path.basename(meta_path)}")
        except Exception as exc:
            self.debug.emit(f"!! could not restore meta keys: {exc}")


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------


def nexus_domain(organizer: mobase.IOrganizer, game_name: str) -> str:
    """Translate an MO2 game name (e.g. 'SkyrimSE') to its Nexus domain."""
    try:
        game = organizer.getGame(game_name)
        if game is not None:
            domain = game.gameNexusName()
            if domain:
                return domain
    except Exception:
        pass
    return game_name.lower()


def mod_url(organizer: mobase.IOrganizer, entry: Entry) -> str:
    if entry.kind != KIND_NEXUS:
        return entry.url
    return f"https://www.nexusmods.com/{nexus_domain(organizer, entry.game)}/mods/{entry.mod_id}"


def write_reports(
    organizer: mobase.IOrganizer, entries: List[Entry], folder: str
) -> List[str]:
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(folder, f"nexus-availability-{stamp}")
    written: List[str] = []

    rows = []
    for entry in entries:
        rows.append(
            {
                "status": entry.status,
                "kind": entry.kind,
                "reason": entry.reason,
                "nexus_status": entry.nexus_status,
                "source": entry.source,
                "game": entry.game,
                "mod_id": entry.mod_id,
                "file_id": entry.file_id or "",
                "mod_name": entry.mod_name or entry.display_name,
                "archive": entry.archive,
                "version": entry.version,
                "url": mod_url(organizer, entry),
            }
        )

    csv_path = f"{base}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["status"])
        writer.writeheader()
        writer.writerows(rows)
    written.append(csv_path)

    json_path = f"{base}.json"
    flagged = [row for row in rows if row["status"] in FLAGGED_STATES]
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "generated": datetime.now().isoformat(timespec="seconds"),
                "checked": len(rows),
                "flagged": len(flagged),
                "entries": rows,
            },
            handle,
            indent=2,
        )
    written.append(json_path)

    md_path = f"{base}.md"
    errors = [row for row in rows if row["status"] == ST_ERROR]
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(f"# Nexus availability report\n\n")
        handle.write(f"Generated {datetime.now().isoformat(timespec='seconds')}\n\n")
        handle.write(
            f"- Entries checked: **{len(rows)}**\n"
            f"- Needing attention: **{len(flagged)}**\n"
            f"- Inconclusive (network/rate limit): **{len(errors)}**\n\n"
        )
        if flagged:
            handle.write("## Verify Nexus availability\n\n")
            handle.write("| Mod | Mod ID | File ID | Archive | Reason |\n")
            handle.write("| --- | --- | --- | --- | --- |\n")
            for row in flagged:
                name = f"[{row['mod_name'] or row['mod_id']}]({row['url']})"
                handle.write(
                    f"| {name} | {row['mod_id']} | {row['file_id']} | "
                    f"{row['archive']} | {row['reason']} |\n"
                )
            handle.write("\n")
        if errors:
            handle.write("## Inconclusive - re-run these\n\n")
            for row in errors:
                handle.write(f"- {row['mod_name'] or row['mod_id']} ({row['url']}): {row['reason']}\n")
    written.append(md_path)

    return written


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class AvailabilityDialog(QDialog):
    COLUMNS = ["Status", "Kind", "Mod", "Mod ID", "File ID", "Archive", "Detail"]

    def __init__(self, organizer: mobase.IOrganizer, plugin_name: str, parent=None) -> None:
        super().__init__(parent)
        self._organizer = organizer
        self._plugin_name = plugin_name
        self._entries: List[Entry] = []
        self._checker: Optional[AvailabilityChecker] = None
        self._url_checker: Optional["UrlChecker"] = None
        self._downloader: Optional["DownloadRunner"] = None
        self._download_hooks = False
        self._cache: Dict[str, Any] = {}

        self.setWindowTitle(PLUGIN_NAME)
        self.setMinimumSize(1100, 700)
        self._init_palette()
        self._build_ui()
        self._load_cache()

    # -- construction ------------------------------------------------------

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

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        options = QGroupBox("What to check")
        grid = QGridLayout(options)

        self._chk_downloads = QCheckBox("Downloads folder (.meta files)")
        self._chk_downloads.setChecked(True)
        self._chk_downloads.setToolTip(
            "Reads every *.meta in the downloads folder with repository=Nexus. "
            "This is what Wabbajack uses to rebuild the list."
        )
        self._chk_mods = QCheckBox("Installed mods with a Nexus ID")
        self._chk_mods.setChecked(bool(self._setting("include_installed_mods", True)))
        self._chk_mods.setToolTip(
            "Catches mods whose archive is no longer in the downloads folder. "
            "Mod-page level only - no file ID to verify."
        )
        self._chk_archive_only = QCheckBox("Only entries whose archive is still on disk")
        self._chk_archive_only.setChecked(False)
        self._chk_verify_files = QCheckBox("Verify the exact file ID (1 extra API call each)")
        self._chk_verify_files.setChecked(True)
        self._chk_verify_files.setToolTip(
            "Mod-page checks alone catch deleted, hidden and moderated mods. This adds "
            "a per-file check that also catches a file the author pulled from a mod "
            "page that is otherwise still up - which still breaks a Wabbajack build."
        )

        self._chk_links = QCheckBox("Check non-Nexus links (directURL / manualURL)")
        self._chk_links.setChecked(True)
        self._chk_links.setToolTip(
            "Validates the URLs in non-Nexus .meta files over plain HTTP. Uses no "
            "Nexus API quota."
        )
        self._chk_whitelist = QCheckBox("Compare links against Wabbajack's server whitelist")
        self._chk_whitelist.setChecked(True)
        self._chk_whitelist.setToolTip(
            "Downloads ServerWhitelist.yml from the wabbajack-tools repo and flags any "
            "directURL whose prefix is not on it - those fail compilation even when the "
            "link works."
        )

        grid.addWidget(self._chk_downloads, 0, 0)
        grid.addWidget(self._chk_mods, 0, 1)
        grid.addWidget(self._chk_archive_only, 1, 0)
        grid.addWidget(self._chk_verify_files, 1, 1)
        grid.addWidget(self._chk_links, 2, 0)
        grid.addWidget(self._chk_whitelist, 2, 1)

        self._chk_restore_meta = QCheckBox("Restore directURL in .meta after downloading")
        self._chk_restore_meta.setChecked(True)
        self._chk_restore_meta.setToolTip(
            "MO2 writes its own .meta for a fresh download and knows nothing about "
            "directURL, so Wabbajack would lose the source. This puts the key back."
        )
        self._chk_unverified = QCheckBox("Fetch missing archives even if not yet checked")
        self._chk_unverified.setChecked(False)
        self._chk_unverified.setToolTip(
            "Normally only archives confirmed to still exist are queued. Tick this to "
            "fetch straight after a scan without running the Nexus check first. "
            "Anything already flagged as removed is still never queued."
        )
        grid.addWidget(self._chk_restore_meta, 3, 0)
        grid.addWidget(self._chk_unverified, 3, 1)
        grid.addWidget(QLabel("Simultaneous downloads:"), 4, 0)
        self._spin_downloads = QSpinBox()
        self._spin_downloads.setRange(1, 8)
        self._spin_downloads.setValue(int(self._setting("simultaneous_downloads", 2)))
        grid.addWidget(self._spin_downloads, 4, 1)

        grid.addWidget(QLabel("Concurrent requests:"), 5, 0)
        self._spin_concurrency = QSpinBox()
        self._spin_concurrency.setRange(1, 16)
        self._spin_concurrency.setValue(int(self._setting("max_concurrent_requests", 4)))
        grid.addWidget(self._spin_concurrency, 5, 1)

        grid.addWidget(QLabel("Skip entries confirmed OK within (days, 0 = never skip):"), 6, 0)
        self._spin_cache = QSpinBox()
        self._spin_cache.setRange(0, 365)
        self._spin_cache.setValue(int(self._setting("cache_days", 7)))
        grid.addWidget(self._spin_cache, 6, 1)

        layout.addWidget(options)

        controls = QHBoxLayout()
        self._btn_scan = QPushButton("Scan MO2")
        self._btn_start = QPushButton("Check Nexus")
        self._btn_stop = QPushButton("Stop")
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(False)
        controls.addWidget(self._btn_scan)
        controls.addWidget(self._btn_start)
        controls.addWidget(self._btn_stop)
        controls.addStretch(1)
        controls.addWidget(QLabel("Show:"))
        self._combo_filter = QComboBox()
        self._combo_filter.addItems(
            ["Everything", "Needs attention", "Errors only", "Archive missing"]
        )
        controls.addWidget(self._combo_filter)
        layout.addLayout(controls)

        self._progress = QProgressBar()
        self._progress.setFormat("%v / %m mod pages")
        layout.addWidget(self._progress)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._table_menu)
        self._table.setSortingEnabled(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)

        self._status_label = QLabel("Ready. Scan MO2 to build the list.")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        log_row = QHBoxLayout()
        self._chk_verbose = QCheckBox("Verbose log (raw bridge traffic)")
        self._chk_verbose.setChecked(True)
        self._btn_test = QPushButton("Test one request")
        self._btn_test.setToolTip(
            "Send a single files request for the first scanned entry and dump "
            "whatever the Nexus bridge sends back."
        )
        self._btn_clear_log = QPushButton("Clear log")
        log_row.addWidget(self._chk_verbose)
        log_row.addStretch(1)
        log_row.addWidget(self._btn_test)
        log_row.addWidget(self._btn_clear_log)
        layout.addLayout(log_row)

        self._log_pane = QPlainTextEdit()
        self._log_pane.setReadOnly(True)
        self._log_pane.setMaximumHeight(150)
        self._log_pane.setPlaceholderText("Diagnostics appear here.")
        layout.addWidget(self._log_pane)

        bottom = QHBoxLayout()
        self._btn_open = QPushButton("Open selected on Nexus")
        self._btn_copy = QPushButton("Copy flagged URLs")
        self._btn_download = QPushButton("Download missing")
        self._btn_download.setEnabled(False)
        self._btn_manual = QPushButton("Copy manual links")
        self._btn_manual.setEnabled(False)
        self._btn_manual.setToolTip(
            "Copies a clickable download link for every missing archive. Nexus links "
            "use the mod-manager download form, which works without Premium."
        )
        self._btn_report = QPushButton("Write report...")
        self._btn_close = QPushButton("Close")
        bottom.addWidget(self._btn_open)
        bottom.addWidget(self._btn_copy)
        bottom.addWidget(self._btn_download)
        bottom.addWidget(self._btn_manual)
        bottom.addStretch(1)
        bottom.addWidget(self._btn_report)
        bottom.addWidget(self._btn_close)
        layout.addLayout(bottom)

        self._btn_scan.clicked.connect(self._do_scan)
        self._btn_start.clicked.connect(self._do_check)
        self._btn_test.clicked.connect(self._do_test)
        self._btn_clear_log.clicked.connect(self._log_pane.clear)
        self._btn_stop.clicked.connect(self._do_stop)
        self._btn_open.clicked.connect(self._open_selected)
        self._btn_copy.clicked.connect(self._copy_flagged)
        self._btn_download.clicked.connect(self._do_download)
        self._btn_manual.clicked.connect(self._copy_manual_links)
        self._btn_report.clicked.connect(self._write_report)
        self._btn_close.clicked.connect(self.accept)
        self._combo_filter.currentIndexChanged.connect(self._apply_filter)
        self._table.doubleClicked.connect(self._open_selected)

    # -- logging -----------------------------------------------------------

    def _log(self, text: str, force: bool = False) -> None:
        if not force and not self._chk_verbose.isChecked() and text.startswith(("<-", "->", "   ")):
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self._log_pane.appendPlainText(f"[{stamp}] {text}")
        qInfo(f"[{PLUGIN_NAME}] {text}")

    def _report_exception(self, what: str, exc: BaseException) -> None:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._log(f"{what} FAILED: {exc}", force=True)
        for line in detail.strip().splitlines():
            self._log(f"    {line}", force=True)
        QMessageBox.warning(
            self,
            PLUGIN_NAME,
            f"{what} failed:\n\n{exc}\n\nFull traceback is in the log pane at the "
            "bottom of this window.",
        )

    def _do_test(self) -> None:
        if not self._entries:
            QMessageBox.information(self, PLUGIN_NAME, "Scan MO2 first.")
            return
        entry = self._entries[0]
        self._log(
            f"Test: requesting description for game={entry.game!r} modID={entry.mod_id}",
            force=True,
        )
        try:
            bridge = self._organizer.createNexusBridge()
            if bridge is None:
                self._log("createNexusBridge() returned None.", force=True)
                return
            self._test_bridge = bridge  # keep a reference alive
            for name in ("descriptionAvailable", "fileInfoAvailable", "requestFailed"):
                try:
                    signal = getattr(bridge, name)
                except TypeError as exc:
                    self._log(f"signal '{name}' unusable from Python: {exc}", force=True)
                    continue
                except AttributeError:
                    self._log(f"bridge has no signal '{name}'", force=True)
                    continue
                signal.connect(
                    lambda *args, _name=name: self._log(
                        f"<- {_name}: "
                        + ", ".join(f"{type(a).__name__}={a!r}"[:140] for a in args),
                        force=True,
                    )
                )
            sent = False
            for user_data in (None, "", 0, []):
                try:
                    bridge.requestDescription(entry.game, entry.mod_id, user_data)
                    self._log(f"request sent with user_data={user_data!r}", force=True)
                    sent = True
                    break
                except (TypeError, ValueError) as exc:
                    self._log(f"rejected with user_data={user_data!r}: {exc}", force=True)
            if not sent:
                self._log("Could not send any form of the request.", force=True)
            else:
                QTimer.singleShot(
                    20000,
                    lambda: self._log(
                        "20s elapsed since the test request - if nothing appeared above, "
                        "MO2 is not dispatching it (check Nexus login under Settings -> Nexus).",
                        force=True,
                    ),
                )
        except Exception as exc:
            self._report_exception("Test request", exc)

    # -- cache -------------------------------------------------------------

    def _data_dir(self) -> str:
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
        folder = self._data_dir()
        try:
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, CACHE_FILE), "w", encoding="utf-8") as handle:
                json.dump(self._cache, handle)
        except Exception as exc:
            qWarning(f"[{PLUGIN_NAME}] could not save cache: {exc}")

    # -- scanning ----------------------------------------------------------

    def _do_scan(self) -> None:
        try:
            self._run_scan()
        except Exception as exc:
            self._report_exception("Scan", exc)

    def _run_scan(self) -> None:
        entries: List[Entry] = []
        if self._chk_downloads.isChecked():
            entries.extend(
                scan_downloads(
                    self._organizer.downloadsPath(),
                    require_archive=self._chk_archive_only.isChecked(),
                )
            )
        if self._chk_mods.isChecked():
            entries.extend(scan_installed_mods(self._organizer))

        entries = dedupe(entries)

        cache_days = self._spin_cache.value()
        cutoff = time.time() - cache_days * 86400 if cache_days > 0 else None
        cached = 0
        if cutoff is not None:
            for entry in entries:
                record = self._cache.get(entry.cache_key)
                if record and float(record.get("checked", 0)) >= cutoff:
                    entry.status = ST_CACHED
                    entry.reason = "Confirmed available on a previous run"
                    cached += 1

        self._entries = entries
        self._populate_table()
        self._refresh_download_button()

        pending = [e for e in entries if e.status == ST_PENDING]
        nexus_pending = [e for e in pending if e.kind == KIND_NEXUS]
        link_pending = [e for e in pending if e.kind in (KIND_DIRECT, KIND_MANUAL)]
        unique_mods = len({e.key for e in nexus_pending})
        file_checks = (
            len({(e.key[0], e.key[1], e.file_id) for e in nexus_pending if e.file_id})
            if self._chk_verify_files.isChecked()
            else 0
        )
        self._btn_start.setEnabled(bool(pending))
        estimate = unique_mods + file_checks
        warning = ""
        if estimate > 2400:
            warning = (
                "  That is close to or over the 2500/day Nexus quota - consider "
                "unticking file verification for the first pass, or run it across "
                "two days (OK results are cached)."
            )
        links_text = (
            f" Plus {len(link_pending)} non-Nexus link(s), which cost no API quota."
            if link_pending
            else ""
        )
        self._status_label.setText(
            f"Found {len(entries)} entries ({cached} skipped from cache). "
            f"Nexus: {unique_mods} mod page(s) + {file_checks} file check(s) "
            f"= about {estimate} API calls.{links_text}{warning}"
        )

    # -- table -------------------------------------------------------------

    def _populate_table(self) -> None:
        self._table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            self._fill_row(row, entry)
        self._table.resizeColumnsToContents()
        self._apply_filter()

    def _init_palette(self) -> None:
        """MO2 ships a dark theme by default, so pick row colours to match
        whatever palette we are actually running under."""
        base = self.palette().color(QPalette.ColorRole.Base)
        self._dark = base.lightness() < 128
        if self._dark:
            self._row_colours = {
                "flag": (QColor("#4a1f26"), QColor("#ffc9cc")),
                "error": (QColor("#463818"), QColor("#ffe0a3")),
                "ok": (QColor("#1d3a26"), QColor("#b8e6c2")),
                "cached": (QColor("#26332c"), QColor("#9dbdaa")),
                "skip": (QColor("#2e2e33"), QColor("#b0b0b8")),
                "busy": (QColor("#1e3348"), QColor("#bcd9f5")),
            }
        else:
            self._row_colours = {
                "flag": (QColor("#ffe1e1"), QColor("#7a1119")),
                "error": (QColor("#fff2d4"), QColor("#6b4600")),
                "ok": (QColor("#e3f5e5"), QColor("#12441f")),
                "cached": (QColor("#eef4ef"), QColor("#3a5442")),
                "skip": (QColor("#f0f0f2"), QColor("#4a4a52")),
                "busy": (QColor("#e3eefb"), QColor("#13395e")),
            }

    def _colour_for(self, entry: Entry) -> Tuple[QColor, QColor]:
        if entry.status == ST_DOWNLOADING:
            return self._row_colours["busy"]
        if entry.status == ST_DOWNLOADED:
            return self._row_colours["ok"]
        if entry.status == ST_DL_FAILED:
            return self._row_colours["error"]
        if entry.flagged:
            return self._row_colours["flag"]
        if entry.status == ST_ERROR:
            return self._row_colours["error"]
        if entry.status == ST_CACHED:
            return self._row_colours["cached"]
        if entry.status == ST_OK:
            return self._row_colours["ok"]
        if entry.status == ST_SKIPPED:
            return self._row_colours["skip"]
        return (QColor(), QColor())

    def _fill_row(self, row: int, entry: Entry) -> None:
        values = [
            entry.status,
            entry.label,
            entry.mod_name or entry.display_name or entry.archive or f"Mod {entry.mod_id}",
            str(entry.mod_id) if entry.mod_id else "-",
            str(entry.file_id) if entry.file_id else "-",
            entry.archive,
            entry.reason,
        ]
        for column, value in enumerate(values):
            item = self._table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                self._table.setItem(row, column, item)
            item.setText(value)
            if column == 6 and entry.url:
                item.setToolTip(f"{entry.url}\n\n{value}")
            elif column == 6:
                item.setToolTip(value)

        if entry.status == ST_PENDING:
            for column in range(len(self.COLUMNS)):
                item = self._table.item(row, column)
                item.setData(Qt.ItemDataRole.BackgroundRole, None)
                item.setData(Qt.ItemDataRole.ForegroundRole, None)
            return

        background, foreground = self._colour_for(entry)
        status_font = self._table.font()
        status_font.setBold(True)
        for column in range(len(self.COLUMNS)):
            item = self._table.item(row, column)
            item.setBackground(background)
            item.setForeground(foreground)
            if column == 0:
                item.setFont(status_font)

    def _apply_filter(self) -> None:
        mode = self._combo_filter.currentIndex()
        for row, entry in enumerate(self._entries):
            if mode == 1:
                visible = entry.flagged
            elif mode == 2:
                visible = entry.status == ST_ERROR
            elif mode == 3:
                visible = archive_missing(entry, self._organizer.downloadsPath())
            else:
                visible = True
            self._table.setRowHidden(row, not visible)

    # -- checking ----------------------------------------------------------

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
        self._log(f"Check requested: {len(pending)} pending entries.", force=True)
        if not pending:
            QMessageBox.information(self, PLUGIN_NAME, "Nothing to check. Scan first.")
            return

        for key, value in (
            ("max_concurrent_requests", self._spin_concurrency.value()),
            ("cache_days", self._spin_cache.value()),
            ("include_installed_mods", self._chk_mods.isChecked()),
        ):
            self._save_setting(key, value)

        self._checker = AvailabilityChecker(
            organizer=self._organizer,
            entries=self._entries,
            max_in_flight=self._spin_concurrency.value(),
            timeout_seconds=int(self._setting("request_timeout_seconds", 45)),
            query_status_on_failure=True,
            verify_file_ids=self._chk_verify_files.isChecked(),
            parent=self,
        )
        self._log(
            "Bridge ready, connected signals: "
            + ", ".join(getattr(self._checker, "_connected", [])),
            force=True,
        )
        self._checker.progressed.connect(self._on_progress)
        self._checker.groupResolved.connect(self._on_group)
        self._checker.logged.connect(self._on_checker_message)
        self._checker.debug.connect(self._log)
        self._checker.finished.connect(self._on_finished)

        self._progress.setRange(0, max(1, self._checker.total_groups))
        self._progress.setValue(0)
        self._btn_start.setEnabled(False)
        self._btn_scan.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._status_label.setText(
            f"Querying Nexus through MO2's linked account "
            f"({self._checker.total_groups} mod pages)..."
        )
        self._log(f"Starting: {self._checker.total_groups} mod pages queued.", force=True)
        self._checker.start()

    def _on_checker_message(self, text: str) -> None:
        self._status_label.setText(text)
        self._log(text, force=True)

    def _do_stop(self) -> None:
        if self._downloader is not None:
            self._downloader.stop()
        if self._url_checker is not None:
            self._url_checker.stop()
        if self._checker is not None:
            self._checker.stop()
        self._btn_stop.setEnabled(False)

    def _on_progress(self, done: int, total: int) -> None:
        self._progress.setValue(done)

    def _on_group(self, indices: List[int]) -> None:
        for index in indices:
            self._fill_row(index, self._entries[index])
        self._apply_filter()

    def _on_finished(self) -> None:
        if self._start_url_phase():
            return
        self._finalize()

    def _pending_url_indices(self) -> List[int]:
        return [
            index
            for index, entry in enumerate(self._entries)
            if entry.kind in (KIND_DIRECT, KIND_MANUAL)
            and entry.status == ST_PENDING
            and entry.url
        ]

    def _start_url_phase(self) -> bool:
        if not self._chk_links.isChecked():
            return False
        indices = self._pending_url_indices()
        if not indices:
            return False
        try:
            self._url_checker = UrlChecker(
                entries=self._entries,
                indices=indices,
                max_in_flight=self._spin_concurrency.value(),
                timeout_seconds=int(self._setting("request_timeout_seconds", 45)),
                check_whitelist=self._chk_whitelist.isChecked(),
                parent=self,
            )
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
        self._status_label.setText(f"Checking {len(indices)} non-Nexus link(s)...")
        self._log(f"Link phase: {len(indices)} URL(s) to check.", force=True)
        self._url_checker.start()
        return True

    def _finalize(self) -> None:
        self._btn_stop.setEnabled(False)
        self._btn_scan.setEnabled(True)
        self._btn_start.setEnabled(
            any(e.status == ST_PENDING for e in self._entries)
        )
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
            f"Done. {flagged} entr{'y' if flagged == 1 else 'ies'} need "
            f"'verify nexus availability', {errors} inconclusive and worth a manual look. "
            "Write a report to hand off to your list build."
        )
        self._refresh_download_button()
        if flagged or errors:
            self._combo_filter.setCurrentIndex(1 if flagged else 2)

    # -- fetching missing archives ----------------------------------------

    def _managed_game(self) -> Tuple[Set[str], str]:
        return managed_game_identifiers(self._organizer)

    def _download_candidates(self) -> Tuple[List[int], Dict[str, int]]:
        """Returns (downloadable indices, {blocker reason: count})."""
        downloads = self._organizer.downloadsPath()
        managed_names, managed_display = self._managed_game()
        indices: List[int] = []
        blocked: Dict[str, int] = {}
        for index, entry in enumerate(self._entries):
            if entry.status in (ST_DOWNLOADING, ST_DOWNLOADED):
                continue
            if not archive_missing(entry, downloads):
                continue
            reason = download_blocker(
                entry,
                managed_names,
                managed_display,
                self._chk_unverified.isChecked(),
            )
            if reason:
                blocked[reason] = blocked.get(reason, 0) + 1
            else:
                indices.append(index)
        return indices, blocked

    def _missing_indices(self) -> List[int]:
        downloads = self._organizer.downloadsPath()
        return [
            index
            for index, entry in enumerate(self._entries)
            if entry.status not in (ST_DOWNLOADING, ST_DOWNLOADED)
            and archive_missing(entry, downloads)
        ]

    def _refresh_download_button(self) -> None:
        try:
            indices, blocked = self._download_candidates()
            missing = self._missing_indices()
        except Exception:
            indices, blocked, missing = [], {}, []
        self._btn_download.setText(f"Download missing ({len(indices)})")
        # Stays clickable whenever anything is missing: a greyed-out button
        # explains nothing, and the dialog itemises why entries were skipped.
        self._btn_download.setEnabled(bool(missing) and self._downloader is None)
        self._btn_manual.setText(f"Copy manual links ({len(missing)})")
        self._btn_manual.setEnabled(bool(missing))
        if missing:
            summary = ", ".join(f"{count} x {reason}" for reason, count in blocked.items())
            self._log(
                f"{len(missing)} archive(s) missing; {len(indices)} can be auto-fetched."
                + (f" Skipped: {summary}" if summary else ""),
                force=True,
            )

    def _ensure_download_hooks(self) -> None:
        """IDownloadManager handlers are permanent, so install them once and
        route them to whichever runner is active."""
        if self._download_hooks:
            return
        manager = self._organizer.downloadManager()
        if manager is None:
            raise RuntimeError("MO2 provided no download manager.")

        def on_complete(download_id: int) -> None:
            if self._downloader is not None:
                self._downloader.handle_complete(download_id)

        def on_failed(download_id: int) -> None:
            if self._downloader is not None:
                self._downloader.handle_failed(download_id)

        manager.onDownloadComplete(on_complete)
        manager.onDownloadFailed(on_failed)
        self._download_hooks = True

    def _do_download(self) -> None:
        try:
            self._start_downloads()
        except Exception as exc:
            self._downloader = None
            self._refresh_download_button()
            self._report_exception("Download", exc)

    def _start_downloads(self) -> None:
        indices, blocked = self._download_candidates()
        if not indices:
            lines = ["Nothing can be fetched automatically right now."]
            if blocked:
                lines += ["", "Missing archives were skipped because:"]
                lines += [
                    f"  - {count} x {reason}" for reason, count in blocked.items()
                ]
            else:
                lines += [
                    "",
                    "No missing archives were found at all. If you deleted a download "
                    "from MO2, its .meta went with it - tick 'Installed mods with a "
                    "Nexus ID' and scan again so the mod is still seen.",
                ]
            QMessageBox.information(self, PLUGIN_NAME, "\n".join(lines))
            return

        nexus_count = sum(
            1 for i in indices if self._entries[i].kind == KIND_NEXUS
        )
        direct_count = len(indices) - nexus_count
        skipped = "\n".join(f"  - {count} x {reason}" for reason, count in blocked.items())

        message = [
            f"Queue {len(indices)} missing archive(s) in MO2?",
            "",
            f"  - {nexus_count} from Nexus (uses your linked account)",
            f"  - {direct_count} from directURL links",
        ]
        if nexus_count:
            message += [
                "",
                "Nexus API downloads are Premium-only. Without Premium MO2 will fail "
                "these and you will have to click through the mod pages instead.",
            ]
        if skipped:
            message += ["", "Not queued:", skipped]
        message += [
            "",
            "Downloads run in MO2's Downloads tab and this can pull a lot of data. "
            "Check your free disk space first.",
        ]

        if (
            QMessageBox.question(
                self,
                PLUGIN_NAME,
                "\n".join(message),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        self._launch_runner(indices)

    def _launch_runner(self, indices: List[int]) -> None:
        self._ensure_download_hooks()
        self._save_setting("simultaneous_downloads", self._spin_downloads.value())

        self._downloader = DownloadRunner(
            organizer=self._organizer,
            entries=self._entries,
            indices=indices,
            max_active=self._spin_downloads.value(),
            restore_meta=self._chk_restore_meta.isChecked(),
            parent=self,
        )
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
        self._status_label.setText(f"Fetching {len(indices)} missing archive(s)...")
        self._log(f"Download phase: {len(indices)} archive(s) queued.", force=True)
        self._downloader.start()

    def _on_downloads_finished(self) -> None:
        done = sum(1 for e in self._entries if e.status == ST_DOWNLOADED)
        failed = sum(1 for e in self._entries if e.status == ST_DL_FAILED)
        self._downloader = None
        self._btn_stop.setEnabled(False)
        self._progress.setFormat("%v / %m")
        self._status_label.setText(
            f"Downloads finished: {done} fetched, {failed} failed. "
            "Anything still running is in MO2's Downloads tab."
        )
        self._refresh_download_button()

    def _copy_manual_links(self) -> None:
        indices = self._missing_indices()
        if not indices:
            QMessageBox.information(self, PLUGIN_NAME, "No missing archives.")
            return
        lines: List[str] = []
        for index in indices:
            entry = self._entries[index]
            url = manual_download_url(self._organizer, entry)
            if not url:
                continue
            name = entry.mod_name or entry.display_name or entry.archive or f"Mod {entry.mod_id}"
            lines.append(f"{name}\t{url}")
        if not lines:
            QMessageBox.information(self, PLUGIN_NAME, "No usable links for the missing archives.")
            return
        QApplication.clipboard().setText("\n".join(lines))
        self._status_label.setText(
            f"Copied {len(lines)} download link(s). Nexus links open the mod-manager "
            "download prompt, which hands the file to MO2 without Premium."
        )

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
        if chosen is None:
            return

        if chosen is open_action:
            for entry in entries[:10]:
                url = manual_download_url(self._organizer, entry)
                if url:
                    QDesktopServices.openUrl(QUrl(url))
        elif chosen is copy_action:
            urls = [manual_download_url(self._organizer, e) for e in entries]
            QApplication.clipboard().setText("\n".join(u for u in urls if u))
            self._status_label.setText(f"Copied {len(urls)} link(s).")
        elif chosen is meta_action:
            paths = [e.meta_path for e in entries if e.meta_path]
            QApplication.clipboard().setText("\n".join(paths))
            self._status_label.setText(f"Copied {len(paths)} .meta path(s).")
        elif chosen is queue_action:
            self._queue_specific(entries)

    def _queue_specific(self, entries: List[Entry]) -> None:
        """Force-queue the selected rows, ignoring the verification rule."""
        if self._downloader is not None:
            QMessageBox.information(self, PLUGIN_NAME, "A download run is already active.")
            return
        managed_names, managed_display = self._managed_game()
        indices: List[int] = []
        blocked: List[str] = []
        for entry in entries:
            reason = download_blocker(
                entry, managed_names, managed_display, allow_unverified=True
            )
            if reason:
                name = entry.mod_name or entry.display_name or entry.archive
                blocked.append(f"{name}: {reason}")
            else:
                indices.append(self._entries.index(entry))
        if not indices:
            QMessageBox.information(
                self,
                PLUGIN_NAME,
                "None of the selected rows can be auto-downloaded:\n\n"
                + "\n".join(blocked[:12])
                + "\n\nUse 'Open download page' instead.",
            )
            return
        self._launch_runner(indices)

    # -- actions -----------------------------------------------------------

    def _selected_entries(self) -> List[Entry]:
        rows = {index.row() for index in self._table.selectedIndexes()}
        return [self._entries[row] for row in sorted(rows) if row < len(self._entries)]

    def _open_selected(self) -> None:
        for entry in self._selected_entries()[:10]:
            url = manual_download_url(self._organizer, entry)
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _copy_flagged(self) -> None:
        urls = sorted(
            {mod_url(self._organizer, e) for e in self._entries if e.flagged}
        )
        if not urls:
            QMessageBox.information(self, PLUGIN_NAME, "Nothing flagged.")
            return
        QApplication.clipboard().setText("\n".join(urls))
        self._status_label.setText(f"Copied {len(urls)} mod page URL(s) to the clipboard.")

    def _write_report(self) -> None:
        if not self._entries:
            QMessageBox.information(self, PLUGIN_NAME, "Nothing to report yet.")
            return
        default = self._data_dir()
        os.makedirs(default, exist_ok=True)
        folder = QFileDialog.getExistingDirectory(self, "Where should the report go?", default)
        if not folder:
            return
        try:
            written = write_reports(self._organizer, self._entries, folder)
        except Exception as exc:
            QMessageBox.warning(self, PLUGIN_NAME, f"Could not write the report:\n{exc}")
            return
        self._status_label.setText(f"Wrote {len(written)} file(s) to {folder}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


class NexusAvailabilityCheck(mobase.IPluginTool):
    def __init__(self) -> None:
        super().__init__()
        self._organizer: Optional[mobase.IOrganizer] = None
        self._parent_widget = None

    # IPlugin
    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        return True

    def name(self) -> str:
        return PLUGIN_NAME

    def author(self) -> str:
        return "Generated for Wabbajack list maintenance"

    def description(self) -> str:
        return (
            "Dry-run download check: asks Nexus (via MO2's linked account) whether "
            "every mod page and file referenced by your downloads still exists, and "
            "reports the ones to verify."
        )

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(*PLUGIN_VERSION, mobase.ReleaseType.BETA)

    def requirements(self) -> List[mobase.IPluginRequirement]:
        return []

    def settings(self) -> List[mobase.PluginSetting]:
        return [
            mobase.PluginSetting(
                "max_concurrent_requests",
                "How many Nexus requests to keep in flight at once.",
                4,
            ),
            mobase.PluginSetting(
                "request_timeout_seconds",
                "Give up on a single Nexus request after this many seconds.",
                45,
            ),
            mobase.PluginSetting(
                "cache_days",
                "Skip entries confirmed available within this many days (0 disables).",
                7,
            ),
            mobase.PluginSetting(
                "include_installed_mods",
                "Also check installed mods that carry a Nexus ID.",
                True,
            ),
        ]

    # IPluginTool
    def displayName(self) -> str:
        return "Nexus Availability Check"

    def tooltip(self) -> str:
        return "Find Nexus mods and files that have been removed"

    def icon(self) -> QIcon:
        return QIcon()

    def setParentWidget(self, widget) -> None:
        self._parent_widget = widget

    def display(self) -> None:
        organizer = self._organizer
        if organizer is None:
            return
        dialog = AvailabilityDialog(organizer, self.name(), self._parent_widget)
        dialog.exec()


def createPlugin() -> mobase.IPlugin:
    return NexusAvailabilityCheck()
