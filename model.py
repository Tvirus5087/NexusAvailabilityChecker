# -*- coding: utf-8 -*-
"""Qt-free scanning of MO2 state: downloads, installed mods, and matching
between the two. Nothing here talks to Nexus."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .common import meta_get, read_ini, set_ini_values, to_bool, to_int

# ---------------------------------------------------------------------------
# Availability-check entries
# ---------------------------------------------------------------------------

ST_PENDING = "PENDING"
ST_OK = "OK"
ST_CACHED = "OK (cached)"
ST_MOD_GONE = "VERIFY NEXUS AVAILABILITY"   # mod page unreachable
ST_FILE_GONE = "VERIFY NEXUS AVAILABILITY"  # file no longer served
ST_ERROR = "ERROR"
ST_SKIPPED = "SKIPPED"
ST_DOWNLOADING = "DOWNLOADING"
ST_DOWNLOADED = "DOWNLOADED"
ST_DL_FAILED = "DOWNLOAD FAILED"

FLAGGED_STATES = {ST_MOD_GONE, ST_FILE_GONE}

KIND_NEXUS = "nexus"
KIND_DIRECT = "directURL"
KIND_MANUAL = "manualURL"
KIND_IPS4 = "ips4"


@dataclass
class Entry:
    """One thing we want to prove is still downloadable."""

    source: str                 # "download" | "mod"
    game: str
    mod_id: int
    file_id: Optional[int]
    kind: str = KIND_NEXUS
    url: str = ""
    prompt: str = ""
    archive: str = ""
    mod_name: str = ""
    display_name: str = ""
    version: str = ""
    meta_path: str = ""
    local_size: Optional[int] = None
    remote_size: Optional[int] = None
    status: str = ST_PENDING
    reason: str = ""
    nexus_status: str = ""
    installed_as: List[str] = field(default_factory=list)  # matched installed mods

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
            if self.source == "mod":
                return "nexus (installed only)"
            return "nexus (+installed)" if self.installed_as else "nexus"
        return self.kind


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

ARCHIVE_EXTS = (".7z", ".zip", ".rar", ".fomod", ".001", ".exe", ".bsa", ".ba2", ".esp", ".esm", ".esl")


@dataclass
class DownloadInfo:
    archive: str
    archive_path: str
    meta_path: str
    has_archive: bool
    has_meta: bool
    size: Optional[int] = None
    game: str = ""
    mod_id: int = 0
    file_id: int = 0
    repository: str = ""
    direct_url: str = ""
    manual_url: str = ""
    prompt: str = ""
    ips4: str = ""
    mod_name: str = ""
    display_name: str = ""
    version: str = ""
    installed_flag: bool = False
    uninstalled_flag: bool = False
    hidden: bool = False

    @property
    def is_nexus(self) -> bool:
        return bool(self.game and self.mod_id > 0) and (
            not self.repository or self.repository.lower() == "nexus"
        )

    @property
    def has_source(self) -> bool:
        return self.is_nexus or bool(self.direct_url or self.manual_url or self.ips4)


def scan_download_folder(downloads_path: str) -> List[DownloadInfo]:
    """Every archive and every .meta in the downloads folder, paired up."""
    out: List[DownloadInfo] = []
    if not downloads_path or not os.path.isdir(downloads_path):
        return out
    names = os.listdir(downloads_path)
    files = {n for n in names if os.path.isfile(os.path.join(downloads_path, n))}
    metas = {n[: -len(".meta")] for n in files if n.lower().endswith(".meta")}
    archives = {
        n for n in files
        if not n.lower().endswith((".meta", ".unfinished", ".wjat.tmp", ".tmp", ".part"))
    }
    for archive in sorted(metas | archives, key=str.lower):
        archive_path = os.path.join(downloads_path, archive)
        meta_path = archive_path + ".meta"
        info = DownloadInfo(
            archive=archive,
            archive_path=archive_path,
            meta_path=meta_path,
            has_archive=archive in archives,
            has_meta=archive in metas,
        )
        if info.has_archive:
            try:
                info.size = os.path.getsize(archive_path)
            except OSError:
                pass
        if info.has_meta:
            meta = read_ini(meta_path)
            info.game = meta_get(meta, "gameName")
            info.mod_id = to_int(meta_get(meta, "modID")) or 0
            info.file_id = to_int(meta_get(meta, "fileID")) or 0
            info.repository = meta_get(meta, "repository")
            info.direct_url = meta_get(meta, "directURL")
            info.manual_url = meta_get(meta, "manualURL")
            info.prompt = meta_get(meta, "prompt")
            info.ips4 = meta_get(meta, "ips4Site")
            info.mod_name = meta_get(meta, "modName")
            info.display_name = meta_get(meta, "name")
            info.version = meta_get(meta, "version")
            info.installed_flag = to_bool(meta_get(meta, "installed"))
            info.uninstalled_flag = to_bool(meta_get(meta, "uninstalled"))
            info.hidden = to_bool(meta_get(meta, "removed"))
        out.append(info)
    return out


# ---------------------------------------------------------------------------
# Installed mods (read straight from the mods folder)
# ---------------------------------------------------------------------------


@dataclass
class InstalledMod:
    name: str
    path: str
    game: str = ""
    mod_id: int = 0
    version: str = ""
    installation_file: str = ""     # basename only
    repository: str = ""
    file_pairs: List[Tuple[int, int]] = field(default_factory=list)  # (modID, fileID)

    @property
    def first_file_id(self) -> Optional[int]:
        for mod_id, file_id in self.file_pairs:
            if mod_id in (0, self.mod_id) and file_id > 0:
                return file_id
        return None


_INSTALLED_FILES_RE = re.compile(r"^\[installedFiles\]\s*$(.*?)(?=^\[|\Z)", re.M | re.S | re.I)


def installed_file_pairs(meta_ini: str) -> List[Tuple[int, int]]:
    """(modID, fileID) pairs from [installedFiles] in a mod's meta.ini."""
    try:
        with open(meta_ini, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return []
    section = _INSTALLED_FILES_RE.search(raw)
    if not section:
        return []
    body = section.group(1)
    mod_ids = {i: int(v) for i, v in re.findall(r"^\s*(\d+)\\+modid\s*=\s*(\d+)", body, re.M | re.I)}
    file_ids = {i: int(v) for i, v in re.findall(r"^\s*(\d+)\\+fileid\s*=\s*(\d+)", body, re.M | re.I)}
    return [
        (mod_ids.get(i, 0), fid)
        for i, fid in sorted(file_ids.items(), key=lambda kv: int(kv[0]))
        if fid > 0
    ]


def scan_mods_folder(mods_path: str, default_game: str = "") -> List[InstalledMod]:
    out: List[InstalledMod] = []
    if not mods_path or not os.path.isdir(mods_path):
        return out
    for name in sorted(os.listdir(mods_path), key=str.lower):
        path = os.path.join(mods_path, name)
        if not os.path.isdir(path) or name.endswith("_separator"):
            continue
        meta_ini = os.path.join(path, "meta.ini")
        meta = read_ini(meta_ini) if os.path.isfile(meta_ini) else {}
        installation = meta_get(meta, "installationFile")
        out.append(
            InstalledMod(
                name=name,
                path=path,
                game=meta_get(meta, "gameName") or default_game,
                mod_id=max(0, to_int(meta_get(meta, "modid")) or 0),
                version=meta_get(meta, "version"),
                installation_file=os.path.basename(installation.replace("\\", "/")) if installation else "",
                repository=meta_get(meta, "repository"),
                file_pairs=installed_file_pairs(meta_ini) if meta else [],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Matching downloads <-> installed mods (no Nexus involved)
# ---------------------------------------------------------------------------


@dataclass
class MatchRow:
    download: DownloadInfo
    mods: List[str] = field(default_factory=list)
    how: str = ""          # "archive name", "Nexus file ID", "both"

    @property
    def matched(self) -> bool:
        return bool(self.mods)


@dataclass
class MatchReport:
    rows: List[MatchRow]
    unmatched_mods: List[InstalledMod]          # installed mods with no archive in downloads
    mods_by_name: Dict[str, InstalledMod]

    @property
    def matched_rows(self) -> List[MatchRow]:
        return [r for r in self.rows if r.matched]


def match_downloads(downloads: List[DownloadInfo], mods: List[InstalledMod]) -> MatchReport:
    by_archive: Dict[str, List[str]] = {}
    by_ids: Dict[Tuple[int, int], List[str]] = {}
    for mod in mods:
        if mod.installation_file:
            by_archive.setdefault(mod.installation_file.lower(), []).append(mod.name)
        for pair_mod, file_id in mod.file_pairs:
            mod_id = pair_mod or mod.mod_id
            if mod_id > 0 and file_id > 0:
                by_ids.setdefault((mod_id, file_id), []).append(mod.name)

    rows: List[MatchRow] = []
    matched_mod_names: Set[str] = set()
    for dl in downloads:
        if not dl.has_archive:
            # A .meta with no archive is an orphan; it cannot back an install.
            rows.append(MatchRow(dl))
            continue
        name_hits = by_archive.get(dl.archive.lower(), [])
        id_hits = by_ids.get((dl.mod_id, dl.file_id), []) if dl.mod_id and dl.file_id else []
        hits = list(dict.fromkeys(name_hits + id_hits))
        how = ""
        if name_hits and id_hits:
            how = "archive name + file ID"
        elif name_hits:
            how = "archive name"
        elif id_hits:
            how = "Nexus file ID"
        rows.append(MatchRow(dl, hits, how))
        matched_mod_names.update(hits)

    unmatched = [m for m in mods if m.name not in matched_mod_names]
    return MatchReport(rows, unmatched, {m.name: m for m in mods})


def plan_flag_changes(
    report: MatchReport, hide_matched: bool, clear_stale: bool
) -> List[Tuple[str, Dict[str, str], str]]:
    """(meta_path, new values, why) for every .meta that needs editing."""
    plan: List[Tuple[str, Dict[str, str], str]] = []
    for row in report.rows:
        dl = row.download
        if not dl.has_meta:
            continue
        if row.matched:
            values: Dict[str, str] = {}
            if not dl.installed_flag:
                values["installed"] = "true"
            if dl.uninstalled_flag:
                values["uninstalled"] = "false"
            if hide_matched and not dl.hidden:
                values["removed"] = "true"
            if values:
                plan.append((dl.meta_path, values, "matched: " + ", ".join(row.mods[:3])))
        elif clear_stale and dl.installed_flag and dl.has_archive:
            plan.append((dl.meta_path, {"installed": "false"}, "no installed mod uses this archive"))
    return plan


def apply_flag_changes(
    plan: List[Tuple[str, Dict[str, str], str]], backup_root: str
) -> Tuple[int, str, List[str]]:
    """Back up every .meta about to change, then edit. Returns (changed,
    backup folder, errors)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(backup_root, f"meta-backup-{stamp}")
    errors: List[str] = []
    changed = 0
    if not plan:
        return 0, "", errors
    os.makedirs(backup_dir, exist_ok=True)
    with open(os.path.join(backup_dir, "source.txt"), "w", encoding="utf-8") as handle:
        handle.write(os.path.dirname(plan[0][0]))
    for meta_path, values, _why in plan:
        try:
            shutil.copy2(meta_path, os.path.join(backup_dir, os.path.basename(meta_path)))
            if set_ini_values(meta_path, values):
                changed += 1
        except Exception as exc:
            errors.append(f"{os.path.basename(meta_path)}: {exc}")
    return changed, backup_dir, errors


def latest_backup(backup_root: str) -> str:
    if not os.path.isdir(backup_root):
        return ""
    folders = sorted(
        (d for d in os.listdir(backup_root) if d.startswith("meta-backup-")), reverse=True
    )
    return os.path.join(backup_root, folders[0]) if folders else ""


def restore_backup(backup_dir: str) -> Tuple[int, List[str]]:
    errors: List[str] = []
    try:
        with open(os.path.join(backup_dir, "source.txt"), "r", encoding="utf-8") as handle:
            target = handle.read().strip()
    except OSError as exc:
        return 0, [f"backup has no source.txt: {exc}"]
    restored = 0
    for name in os.listdir(backup_dir):
        if not name.lower().endswith(".meta"):
            continue
        try:
            shutil.copy2(os.path.join(backup_dir, name), os.path.join(target, name))
            restored += 1
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return restored, errors


# ---------------------------------------------------------------------------
# Entries for the availability check
# ---------------------------------------------------------------------------


def entries_from_downloads(report: MatchReport, require_archive: bool) -> List[Entry]:
    entries: List[Entry] = []
    for row in report.rows:
        dl = row.download
        if not dl.has_meta:
            continue
        if require_archive and not dl.has_archive:
            continue
        common: Dict[str, Any] = dict(
            source="download",
            archive=dl.archive,
            mod_name=dl.mod_name,
            display_name=dl.display_name,
            version=dl.version,
            meta_path=dl.meta_path,
            local_size=dl.size,
            installed_as=list(row.mods),
        )
        if dl.is_nexus:
            entries.append(Entry(game=dl.game, mod_id=dl.mod_id,
                                 file_id=dl.file_id or None, kind=KIND_NEXUS, **common))
        elif dl.direct_url:
            entries.append(Entry(game=dl.game, mod_id=0, file_id=None,
                                 kind=KIND_DIRECT, url=dl.direct_url, **common))
        elif dl.manual_url:
            entries.append(Entry(game=dl.game, mod_id=0, file_id=None, kind=KIND_MANUAL,
                                 url=dl.manual_url, prompt=dl.prompt, **common))
        elif dl.ips4:
            entry = Entry(game=dl.game, mod_id=0, file_id=None, kind=KIND_IPS4, **common)
            entry.status = ST_SKIPPED
            entry.reason = (
                f"ips4 meta ({dl.ips4}) - Wabbajack has deprecated this source type; "
                "convert it to manualURL"
            )
            entries.append(entry)
    return entries


def entries_from_unmatched_mods(
    report: MatchReport, mods: Optional[Iterable[InstalledMod]] = None
) -> List[Entry]:
    """Installed Nexus mods whose archive is NOT in the downloads folder.

    Matched mods are deliberately left out: their download entry already
    covers them, so they cost zero extra API calls. Pass `mods` to check a
    different set (e.g. every installed mod when downloads are not scanned).
    """
    entries: List[Entry] = []
    for mod in (report.unmatched_mods if mods is None else mods):
        if mod.mod_id <= 0 or not mod.game:
            continue
        if mod.repository and mod.repository.lower() != "nexus":
            continue
        entries.append(
            Entry(
                source="mod",
                game=mod.game,
                mod_id=mod.mod_id,
                file_id=mod.first_file_id,
                archive=mod.installation_file,
                mod_name=mod.name,
                display_name=mod.name,
                version=mod.version,
            )
        )
    return entries


def dedupe(entries: Iterable[Entry]) -> List[Entry]:
    entries = list(entries)
    download_files = {
        (e.game.lower(), e.mod_id, e.file_id)
        for e in entries if e.source == "download" and e.kind == KIND_NEXUS
    }
    download_mods = {(g, m) for g, m, _f in download_files}
    seen: Set[Tuple[Any, ...]] = set()
    out: List[Entry] = []
    for entry in entries:
        if entry.source == "mod" and entry.kind == KIND_NEXUS:
            if (entry.game.lower(), entry.mod_id, entry.file_id) in download_files:
                continue
            if entry.file_id is None and (entry.game.lower(), entry.mod_id) in download_mods:
                continue
        marker = (entry.kind, entry.game.lower(), entry.mod_id, entry.file_id, entry.url, entry.source)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(entry)
    return out


def archive_missing(entry: Entry, downloads_path: str) -> bool:
    if entry.source == "mod":
        return not (entry.archive and os.path.isfile(os.path.join(downloads_path, entry.archive)))
    return not os.path.isfile(os.path.join(downloads_path, entry.archive))


# ---------------------------------------------------------------------------
# Nexus request planning
# ---------------------------------------------------------------------------


@dataclass
class RequestPlan:
    mod_groups: Dict[Tuple[str, int], List[int]]
    file_groups: Dict[Tuple[str, int, int], List[int]]
    need_description: Set[Tuple[str, int]]   # mods that get a mod-page request
    game_of: Dict[Tuple[str, int], str]

    @property
    def first_wave(self) -> List[Tuple[str, int, Optional[int]]]:
        """Mod-page requests, plus file requests for mods that skip the page."""
        wave: List[Tuple[str, int, Optional[int]]] = [(g, m, None) for g, m in self.need_description]
        wave += [k for k in self.file_groups if (k[0], k[1]) not in self.need_description]
        return wave

    @property
    def deferred_files(self) -> List[Tuple[str, int, int]]:
        """File requests that wait for their mod page to come back OK."""
        return [k for k in self.file_groups if (k[0], k[1]) in self.need_description]

    @property
    def total_calls(self) -> int:
        return len(self.need_description) + len(self.file_groups)


def plan_requests(entries: List[Entry], verify_files: bool, confirm_mod_pages: bool) -> RequestPlan:
    """One request per unique Nexus file when the file ID is known.

    A file-info request already fails for a deleted file or a pulled mod, so the
    mod page is only asked for when there is no file ID to check, or when the
    author explicitly wants page status (hidden/moderated) as well.
    """
    mod_groups: Dict[Tuple[str, int], List[int]] = {}
    file_groups: Dict[Tuple[str, int, int], List[int]] = {}
    game_of: Dict[Tuple[str, int], str] = {}
    need: Set[Tuple[str, int]] = set()
    for index, entry in enumerate(entries):
        if entry.kind != KIND_NEXUS or entry.status in (ST_CACHED, ST_SKIPPED) or entry.mod_id <= 0:
            continue
        key = entry.key
        mod_groups.setdefault(key, []).append(index)
        game_of.setdefault(key, entry.game)
        if verify_files and entry.file_id:
            file_groups.setdefault((key[0], key[1], entry.file_id), []).append(index)
        else:
            need.add(key)
    if confirm_mod_pages:
        need.update(mod_groups)
    return RequestPlan(mod_groups, file_groups, need, game_of)
