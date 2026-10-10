# -*- coding: utf-8 -*-
"""Install collection archives into MO2 mod folders. Qt-free.

Original implementation:
  * extraction is delegated to an Extractor (7-Zip on Windows),
  * FOMOD archives are installed by executing ModuleConfig.xml with the
    curator's recorded choices (collection.json "choices"), falling back to
    the installer's own defaults when no choices were recorded,
  * other archives are installed by locating their Data root.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


class ExtractError(RuntimeError):
    pass


def long_path(path: str) -> str:
    """Windows extended-length path, so deep mod files are not cut off by the
    260-character MAX_PATH limit. No-op elsewhere."""
    if os.name != "nt" or not path:
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def short_path(path: str) -> str:
    """Strip the extended-length prefix (for external tools and messages)."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


# Vortex mod types that deploy to the game folder, not Data.
ROOT_MOD_TYPES = {"dinput", "enb", "root", "gamebryo-root"}


class SevenZipExtractor:
    """7-Zip handles 7z, zip and rar (incl. RAR5)."""

    def __init__(self, exe: str) -> None:
        self.exe = exe

    def extract(self, archive: str, out_dir: str) -> None:
        os.makedirs(out_dir, exist_ok=True)
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        proc = subprocess.run(
            [self.exe, "x", short_path(archive), f"-o{short_path(out_dir)}", "-y", "-bso0", "-bsp0", "-aoa"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        if proc.returncode not in (0, 1):  # 1 = warnings
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise ExtractError(f"7-Zip exit {proc.returncode}: {err[:300]}")


class ZipExtractor:
    """Fallback / test extractor (zip only)."""

    def extract(self, archive: str, out_dir: str) -> None:
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(out_dir)
        except zipfile.BadZipFile as exc:
            raise ExtractError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Case-insensitive path helpers (archives are authored on Windows)
# ---------------------------------------------------------------------------


def _split(rel: str) -> List[str]:
    return [p for p in re.split(r"[\\/]+", rel or "") if p and p != "."]


def resolve_ci(base: str, rel: str) -> Optional[str]:
    """Find base/rel ignoring case; None if missing."""
    current = base
    for part in _split(rel):
        direct = os.path.join(current, part)
        if os.path.exists(direct):
            current = direct
            continue
        try:
            names = os.listdir(current)
        except OSError:
            return None
        lowered = part.lower()
        hit = next((n for n in names if n.lower() == lowered), None)
        if hit is None:
            return None
        current = os.path.join(current, hit)
    return current


def _walk_files(root: str) -> Iterable[Tuple[str, str]]:
    """(absolute path, path relative to root) for every file."""
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            yield full, os.path.relpath(full, root)


# ---------------------------------------------------------------------------
# Data-root detection for non-FOMOD archives
# ---------------------------------------------------------------------------

DATA_DIRS = {
    "meshes", "textures", "scripts", "interface", "skse", "sound", "music", "seq", "strings",
    "shadersfx", "lodsettings", "grass", "video", "dyndolod", "nemesis_engine", "source",
    "calientetools", "netscriptframework", "shaders", "facegen", "materials", "lightplacer",
    "pbrnifpatcher", "pbrtexturesets", "dialogueviews", "platform", "tools", "terrain",
    "particlelights", "mcm", "dlcintegration", "keywords", "distantdetail",
}
DATA_EXTS = {".esp", ".esm", ".esl", ".bsa", ".ba2", ".ini", ".modgroups", ".swf"}
ROOT_EXTS = {".exe", ".dll", ".asi"}
JUNK = {"fomod", "readme", "docs", "documentation", "screenshots", "images", "__macosx"}


def _looks_like_data(entries: List[str], folder: str) -> bool:
    for name in entries:
        lowered = name.lower()
        if os.path.isdir(os.path.join(folder, name)):
            if lowered in DATA_DIRS:
                return True
        elif os.path.splitext(lowered)[1] in DATA_EXTS:
            return True
    return False


def override_mod_type(root: str) -> str:
    """Vortex's vortex_override_instructions.json ('setmodtype') if present."""
    import json
    for depth_root, dirs, files in os.walk(root):
        if os.path.relpath(depth_root, root).count(os.sep) > 1:
            dirs[:] = []
            continue
        for name in files:
            if name.lower() == "vortex_override_instructions.json":
                try:
                    with open(os.path.join(depth_root, name), "r", encoding="utf-8-sig") as h:
                        data = json.load(h)
                    for item in data if isinstance(data, list) else [data]:
                        if isinstance(item, dict) and item.get("type") == "setmodtype":
                            return str(item.get("value") or "").lower()
                except (OSError, ValueError):
                    return ""
    return ""


def root_only(folder: str) -> bool:
    """Top level has game-root executables/DLLs and nothing Data-like."""
    try:
        entries = os.listdir(folder)
    except OSError:
        return False
    if _looks_like_data(entries, folder):
        return False
    return any(os.path.splitext(e.lower())[1] in ROOT_EXTS for e in entries
               if os.path.isfile(os.path.join(folder, e)))


def _leads_to_data(folder: str, depth: int = 3) -> bool:
    """True if folder, or a chain of single sub-folders under it, contains a
    Data folder or Data-like content."""
    for _ in range(depth):
        try:
            entries = os.listdir(folder)
        except OSError:
            return False
        if _looks_like_data(entries, folder) or any(
                e.lower() == "data" and os.path.isdir(os.path.join(folder, e)) for e in entries):
            return True
        dirs = [e for e in entries if os.path.isdir(os.path.join(folder, e)) and e.lower() not in JUNK]
        if len(dirs) != 1:
            return False
        folder = os.path.join(folder, dirs[0])
    return False


def _root_base(root: str) -> str:
    """Unwrap single wrapper folders above a game-root mod's files."""
    current = root
    for _ in range(3):
        try:
            entries = os.listdir(current)
        except OSError:
            break
        dirs = [e for e in entries if os.path.isdir(os.path.join(current, e)) and e.lower() not in JUNK]
        files = [e for e in entries if os.path.isfile(os.path.join(current, e))
                 and not e.lower().endswith((".txt", ".md", ".url", ".json", ".html", ".pdf"))]
        if len(dirs) == 1 and not files and dirs[0].lower() != "data":
            current = os.path.join(current, dirs[0])
            continue
        break
    return current


def find_data_root(root: str) -> Tuple[str, Optional[str], str]:
    """(data root, game-root folder or None, note)."""
    current = root
    for _depth in range(4):
        try:
            entries = [e for e in os.listdir(current) if e.lower() not in ("meta.ini",)]
        except OSError:
            break
        lowered = {e.lower(): e for e in entries}
        if "data" in lowered and os.path.isdir(os.path.join(current, lowered["data"])):
            game_root = current
            has_root_files = any(os.path.splitext(e.lower())[1] in ROOT_EXTS for e in entries)
            return (os.path.join(current, lowered["data"]),
                    game_root if has_root_files else None, "")
        if _looks_like_data(entries, current):
            return current, None, ""
        dirs = [e for e in entries if os.path.isdir(os.path.join(current, e)) and e.lower() not in JUNK]
        files = [e for e in entries if os.path.isfile(os.path.join(current, e))]
        meaningful_files = [f for f in files if not f.lower().endswith((".txt", ".md", ".pdf", ".url", ".jpg", ".png", ".html", ".htm", ".rtf"))]
        # Only unwrap a single folder when something Data-like is actually
        # inside it. A lone unknown folder (e.g. "OSLAroused/") is far more
        # often a real Data subfolder than a wrapper, so it stays as-is.
        if len(dirs) == 1 and not meaningful_files and _leads_to_data(os.path.join(current, dirs[0])):
            current = os.path.join(current, dirs[0])
            continue
        break
    if current != root:
        return current, None, ""
    return root, None, "no standard Data folders at the top - installed as-is (archive root = Data)"


# ---------------------------------------------------------------------------
# FOMOD
# ---------------------------------------------------------------------------


def _strip_ns(elem: ET.Element) -> ET.Element:
    for e in elem.iter():
        if isinstance(e.tag, str) and "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    return elem


def find_fomod(root: str) -> Optional[Tuple[str, str]]:
    """(ModuleConfig.xml path, fomod base dir) - the base dir is the folder
    that *contains* the fomod folder; sources are relative to it."""
    for depth_root, dirs, files in os.walk(root):
        depth = os.path.relpath(depth_root, root).count(os.sep)
        if depth > 2:
            dirs[:] = []
            continue
        if os.path.basename(depth_root).lower() == "fomod":
            for name in files:
                if name.lower() == "moduleconfig.xml":
                    return os.path.join(depth_root, name), os.path.dirname(depth_root)
    return None


def load_module_config(path: str) -> ET.Element:
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        return _strip_ns(ET.fromstring(raw))
    except ET.ParseError:
        pass
    # Some installers declare one encoding and use another.
    for encoding in ("utf-16", "utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
            return _strip_ns(ET.fromstring(text))
        except (UnicodeDecodeError, ET.ParseError):
            continue
    raise ExtractError("ModuleConfig.xml could not be parsed")


@dataclass
class FileOp:
    source: str          # path relative to the fomod base dir
    destination: str     # path relative to Data
    is_folder: bool
    priority: int = 0
    order: int = 0


@dataclass
class FomodResult:
    ops: List[FileOp]
    flags: Dict[str, str]
    mode: str            # "choices" | "defaults" | "mixed"
    notes: List[str] = field(default_factory=list)


def _file_ops(files_elem: Optional[ET.Element], counter: List[int], only_always: bool = False) -> List[FileOp]:
    ops: List[FileOp] = []
    if files_elem is None:
        return ops
    for item in files_elem:
        if item.tag not in ("file", "folder"):
            continue
        if only_always and str(item.get("alwaysInstall", "false")).lower() != "true":
            continue
        source = item.get("source", "")
        is_folder = item.tag == "folder"
        if "destination" in item.attrib:
            dest = item.get("destination") or ""
            if not is_folder and (dest == "" or dest.endswith(("/", "\\"))):
                dest = dest + _split(source)[-1] if _split(source) else dest
        else:
            dest = "" if is_folder else source
        try:
            priority = int(item.get("priority", "0"))
        except ValueError:
            priority = 0
        counter[0] += 1
        ops.append(FileOp(source, dest, is_folder, priority, counter[0]))
    return ops


class FomodRunner:
    def __init__(self, config: ET.Element, file_state: Callable[[str], str]) -> None:
        self.config = config
        self.file_state = file_state   # plugin name -> "Active" | "Inactive" | "Missing"
        self.flags: Dict[str, str] = {}
        self.counter = [0]
        self.notes: List[str] = []

    # -- conditions --------------------------------------------------------

    def _deps_ok(self, deps: Optional[ET.Element]) -> bool:
        if deps is None:
            return True
        operator = str(deps.get("operator", "And")).lower()
        results: List[bool] = []
        for child in deps:
            tag = child.tag
            if tag == "flagDependency":
                results.append(self.flags.get(child.get("flag", ""), "") == (child.get("value") or ""))
            elif tag == "fileDependency":
                want = str(child.get("state", "Active")).lower()
                have = self.file_state(child.get("file", "")).lower()
                results.append(have == want)
            elif tag == "dependencies":
                results.append(self._deps_ok(child))
            else:  # gameDependency, foseDependency, fommDependency: assume satisfied
                results.append(True)
        if not results:
            return True
        return any(results) if operator == "or" else all(results)

    def _plugin_type(self, plugin: ET.Element) -> str:
        td = plugin.find("typeDescriptor")
        if td is None:
            return "Optional"
        simple = td.find("type")
        if simple is not None:
            return simple.get("name", "Optional")
        dep = td.find("dependencyType")
        if dep is not None:
            for pattern in dep.findall("patterns/pattern"):
                if self._deps_ok(pattern.find("dependencies")):
                    t = pattern.find("type")
                    if t is not None:
                        return t.get("name", "Optional")
            default = dep.find("defaultType")
            if default is not None:
                return default.get("name", "Optional")
        return "Optional"

    # -- selection ---------------------------------------------------------

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip()).lower()

    def _choose(self, group: ET.Element, recorded: Optional[List[Dict[str, Any]]]) -> Tuple[List[ET.Element], bool]:
        """(selected plugins, used recorded choices?)"""
        plugins = group.findall("plugins/plugin")
        gtype = group.get("type", "SelectAny")
        if gtype == "SelectAll":
            return plugins, recorded is not None
        if recorded is not None:
            chosen: List[ET.Element] = []
            for choice in recorded:
                name = self._norm(choice.get("name", ""))
                hit = next((p for p in plugins if self._norm(p.get("name", "")) == name), None)
                if hit is None:
                    idx = choice.get("idx")
                    if isinstance(idx, int) and 0 <= idx < len(plugins):
                        hit = plugins[idx]
                        self.notes.append(f"choice '{choice.get('name')}' matched by position")
                if hit is None:
                    self.notes.append(f"recorded choice '{choice.get('name')}' not found in group '{group.get('name')}'")
                elif hit not in chosen:
                    chosen.append(hit)
            return chosen, True
        types = {id(p): self._plugin_type(p) for p in plugins}
        chosen = [p for p in plugins if types[id(p)] in ("Required", "Recommended")]
        usable = [p for p in plugins if types[id(p)] != "NotUsable"]
        if gtype in ("SelectExactlyOne", "SelectAtMostOne"):
            chosen = chosen[:1]
            if not chosen and gtype == "SelectExactlyOne" and usable:
                chosen = [usable[0]]
        elif gtype == "SelectAtLeastOne" and not chosen and usable:
            chosen = [usable[0]]
        return chosen, False

    def run(self, choices: Optional[Dict[str, Any]]) -> FomodResult:
        cfg = self.config
        ops: List[FileOp] = _file_ops(cfg.find("requiredInstallFiles"), self.counter)
        recorded_steps: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        if choices:
            for step in choices.get("options") or []:
                groups = recorded_steps.setdefault(self._norm(step.get("name", "")), {})
                for group in step.get("groups") or []:
                    groups[self._norm(group.get("name", ""))] = list(group.get("choices") or [])
        used_recorded = used_defaults = False
        for step in cfg.findall("installSteps/installStep"):
            step_key = self._norm(step.get("name", ""))
            step_rec = recorded_steps.get(step_key)
            visible = self._deps_ok(step.find("visible"))
            if not visible and step_rec is None:
                continue
            for group in step.findall("optionalFileGroups/group"):
                recorded = step_rec.get(self._norm(group.get("name", ""))) if step_rec is not None else None
                if step_rec is not None and recorded is None:
                    recorded = []   # step was shown, nothing picked in this group
                selected, from_record = self._choose(group, recorded)
                used_recorded |= from_record
                used_defaults |= not from_record and group.get("type") != "SelectAll"
                for plugin in group.findall("plugins/plugin"):
                    if plugin in selected:
                        for flag in plugin.findall("conditionFlags/flag"):
                            self.flags[flag.get("name", "")] = (flag.text or "").strip()
                        ops += _file_ops(plugin.find("files"), self.counter)
                    else:
                        ops += _file_ops(plugin.find("files"), self.counter, only_always=True)
        for pattern in cfg.findall("conditionalFileInstalls/patterns/pattern"):
            if self._deps_ok(pattern.find("dependencies")):
                ops += _file_ops(pattern.find("files"), self.counter)
        if choices and recorded_steps and not used_recorded:
            self.notes.append("recorded choices did not match this installer's steps")
        mode = "choices" if used_recorded and not used_defaults else (
            "mixed" if used_recorded else "defaults")
        return FomodResult(ops, dict(self.flags), mode, self.notes)


def apply_ops(ops: List[FileOp], base: str, target: str) -> Tuple[int, List[str]]:
    """Copy FOMOD file ops (lowest priority first, so higher wins)."""
    missing: List[str] = []
    count = 0
    for op in sorted(ops, key=lambda o: (o.priority, o.order)):
        src = resolve_ci(base, op.source) if op.source else base
        if src is None:
            missing.append(op.source)
            continue
        dest = os.path.join(target, *_split(op.destination))
        if op.is_folder or os.path.isdir(src):
            for full, rel in _walk_files(src):
                out = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                shutil.copy2(full, out)
                count += 1
        else:
            os.makedirs(os.path.dirname(dest) or target, exist_ok=True)
            shutil.copy2(src, dest)
            count += 1
    return count, missing


# ---------------------------------------------------------------------------
# One mod
# ---------------------------------------------------------------------------


@dataclass
class InstallJob:
    key: str
    mod_name: str              # MO2 folder name to create
    archive: str               # absolute archive path ("" for bundled folder)
    game: str = ""
    mod_id: int = 0
    file_id: int = 0
    version: str = ""
    category: str = ""
    choices: Optional[Dict[str, Any]] = None
    md5: str = ""
    hashes: Optional[List[Dict[str, Any]]] = None
    has_patches: bool = False
    bundled_dir: str = ""      # for bundled sources: folder to copy as the mod
    mod_type: str = ""         # Vortex mod type from collection.json details.type
    nexus_url: str = ""


@dataclass
class InstallResult:
    key: str
    mod_name: str
    ok: bool
    files: int = 0
    mode: str = ""             # fomod choices/defaults/mixed, "plain", "bundled", "existing"
    notes: List[str] = field(default_factory=list)


STAGING_SUFFIX = ".wjat-installing"
MAX_FOLDER = 80   # keeps real paths short enough for the game and MO2's VFS


def safe_folder_name(name: str, suffix: str = "") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    room = MAX_FOLDER - len(suffix)
    if len(cleaned) > room:
        cleaned = cleaned[:room].rstrip(" -_.")
    return (cleaned or "Unnamed mod") + suffix


def cleanup_staging(mods_path: str) -> List[str]:
    """Remove half-installed folders left by an interrupted or failed build."""
    removed = []
    try:
        names = os.listdir(mods_path)
    except OSError:
        return removed
    for name in names:
        if name.endswith(STAGING_SUFFIX):
            shutil.rmtree(long_path(os.path.join(mods_path, name)), ignore_errors=True)
            removed.append(name)
    return removed


def md5_of(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_meta_ini(mod_dir: str, job: InstallJob) -> None:
    lines = [
        "[General]",
        f"gameName={job.game}",
        f"modid={job.mod_id}",
        f"version={job.version}",
        f"installationFile={os.path.basename(job.archive) if job.archive else ''}",
        f"repository={'Nexus' if job.mod_id else ''}",
        f"url={job.nexus_url}",
        "comments=Installed from a Nexus collection by Wabbajack Author Toolkit",
        "",
        "[installedFiles]",
    ]
    if job.mod_id and job.file_id:
        lines += [f"1\\modid={job.mod_id}", f"1\\fileid={job.file_id}", "size=1"]
    else:
        lines += ["size=0"]
    with open(os.path.join(mod_dir, "meta.ini"), "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write("\n".join(lines) + "\n")


def install_job(job: InstallJob, mods_path: str, work_dir: str, extractor,
                file_state: Callable[[str], str], verify_md5: bool = False) -> InstallResult:
    result = InstallResult(job.key, job.mod_name, ok=False)
    final_dir = long_path(os.path.join(mods_path, job.mod_name))
    staging = final_dir + STAGING_SUFFIX
    extract_dir = long_path(os.path.join(work_dir, re.sub(r"\W+", "_", job.key)))
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        if job.bundled_dir:
            source_root = long_path(job.bundled_dir)
            result.mode = "bundled"
        else:
            if verify_md5 and job.md5:
                actual = md5_of(job.archive)
                if actual.lower() != job.md5.lower():
                    result.notes.append(f"archive MD5 {actual} differs from the curator's {job.md5}")
            extractor.extract(long_path(job.archive), extract_dir)
            source_root = extract_dir

        fomod = find_fomod(source_root) if not job.hashes else None
        if job.hashes:
            count, notes = _install_by_hashes(job.hashes, source_root, staging)
            result.files, result.mode = count, "exact files"
            result.notes += notes
        elif fomod is not None:
            config_path, base = fomod
            runner = FomodRunner(load_module_config(config_path), file_state)
            fres = runner.run(job.choices)
            count, missing = apply_ops(fres.ops, base, staging)
            result.files, result.mode = count, f"fomod {fres.mode}"
            result.notes += fres.notes
            if missing:
                result.notes.append(f"{len(missing)} FOMOD source(s) not in archive: {', '.join(missing[:5])}")
            if fres.mode != "choices" and job.choices is None:
                result.notes.append("no recorded choices - installer defaults used, review this mod")
        elif (job.mod_type.lower() in ROOT_MOD_TYPES
              or override_mod_type(source_root) in ROOT_MOD_TYPES
              or root_only(find_data_root(source_root)[0])):
            wrapper = _root_base(source_root)
            count = 0
            for full, rel in _walk_files(wrapper):
                lowered = rel.lower()
                if lowered in ("meta.ini", "vortex_override_instructions.json"):
                    continue
                parts = _split(rel)
                if parts and parts[0].lower() == "data" and len(parts) > 1:
                    # Root Builder ignores a mod with Root/Data, so Data files
                    # stay at the mod's top level where MO2 serves them.
                    out = os.path.join(staging, *parts[1:])
                else:
                    out = os.path.join(staging, "Root", rel)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                shutil.copy2(full, out)
                count += 1
            result.files, result.mode = count, "game root"
            result.notes.append("game-root files placed in Root/ - Root Builder deploys them to the game "
                                "(or Stock Game) folder")
        else:
            data_root, game_root, note = find_data_root(source_root)
            if job.choices:
                result.notes.append("curator recorded FOMOD choices but the archive has no fomod folder")
            count = 0
            from .rootbuilder import is_injector_entry
            injected = {e.lower() for e in os.listdir(data_root)
                        if is_injector_entry(e, os.path.isdir(os.path.join(data_root, e)))}
            for full, rel in _walk_files(data_root):
                if rel.lower().startswith("fomod" + os.sep) or rel.lower() == "meta.ini":
                    continue
                top = _split(rel)[0].lower()
                # proxy DLLs, .asi, loaders, ENB files: game folder only
                out = os.path.join(staging, "Root", rel) if top in injected else os.path.join(staging, rel)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                shutil.copy2(full, out)
                count += 1
            if injected:
                result.notes.append("game-folder files (" + ", ".join(sorted(injected)[:4])
                                    + ("…" if len(injected) > 4 else "") + ") placed in Root/ for Root Builder")
            if game_root:
                for entry in os.listdir(game_root):
                    full = os.path.join(game_root, entry)
                    if entry.lower() == "data":
                        continue
                    target = os.path.join(staging, "Root", entry)
                    if os.path.isdir(full):
                        shutil.copytree(full, target, dirs_exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        shutil.copy2(full, target)
                    count += 1
                result.notes.append("game-root files placed in Root/ - Root Builder deploys them to the game "
                                "(or Stock Game) folder")
            result.files = count
            result.mode = result.mode or "plain"
            if note:
                result.notes.append(note)
        if job.has_patches:
            result.notes.append("curator patches files in this mod - patches are not applied yet")
        if result.files == 0:
            result.notes.append("installed no files")
        write_meta_ini(staging, job)
        if os.path.isdir(final_dir):
            shutil.rmtree(final_dir)
        os.replace(staging, final_dir)
        result.ok = result.files > 0
    except Exception as exc:  # report per mod, keep going
        result.notes.append(f"{type(exc).__name__}: {exc}")
        shutil.rmtree(staging, ignore_errors=True)
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
    return result


def _install_by_hashes(hashes: List[Dict[str, Any]], root: str, target: str) -> Tuple[int, List[str]]:
    """'Replicate' installs: the curator listed every file (path + md5)."""
    by_md5: Dict[str, str] = {}
    for full, _rel in _walk_files(root):
        try:
            by_md5.setdefault(md5_of(full), full)
        except OSError:
            continue
    count, missing = 0, []
    for item in hashes:
        rel = str(item.get("path") or "")
        digest = str(item.get("md5") or "").lower()
        src = by_md5.get(digest) or resolve_ci(root, rel)
        if not src or not os.path.isfile(src):
            missing.append(rel)
            continue
        out = os.path.join(target, *_split(rel))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(src, out)
        count += 1
    notes = [f"{len(missing)} listed file(s) not found in the archive"] if missing else []
    return count, notes


def plugin_state_from(folders: Iterable[str]) -> Callable[[str], str]:
    """file_state for FOMOD fileDependency: plugins present in the game Data
    folder or any already-installed mod count as Active."""
    present: Set[str] = set()
    for folder in folders:
        try:
            for name in os.listdir(folder):
                if name.lower().endswith((".esp", ".esm", ".esl")):
                    present.add(name.lower())
        except OSError:
            continue

    def state(name: str) -> str:
        return "Active" if os.path.basename(name).lower() in present else "Missing"

    state.present = present  # type: ignore[attr-defined]
    return state
