# -*- coding: utf-8 -*-
"""Root Builder detection and installation helpers. Qt-free.

Root Builder (by Kezyma) deploys each mod's `Root/` folder into the game
folder when MO2 launches something. Without it, files the toolkit places in
`Root/` (SKSE loaders, ENB, preloaders, tools) never reach the game.

Downloads come from the author's own GitHub release tag; the user confirms
first, and only the plugin folders found in that zip are written.
"""

from __future__ import annotations

import os
import re
import zipfile
from typing import Iterable, List, Optional, Tuple

RELEASE_ASSETS_URL = "https://github.com/Kezyma/ModOrganizer-Plugins/releases/expanded_assets/rootbuilder"
RELEASE_PAGE_URL = "https://github.com/Kezyma/ModOrganizer-Plugins/releases/tag/rootbuilder"
NEXUS_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/31720"
DOCS_URL = "https://github.com/Kezyma/ModOrganizer-Plugins/blob/main/docs/rootbuilder.md"
PLUGIN_NAME = "Root Builder"

_ASSET_RE = re.compile(
    r"/Kezyma/ModOrganizer-Plugins/releases/download/rootbuilder/(rootbuilder\.(\d+(?:\.\d+)*)\.zip)")


def is_installed(plugins_dir: str) -> bool:
    if not plugins_dir or not os.path.isdir(plugins_dir):
        return False
    for name in os.listdir(plugins_dir):
        lowered = name.lower()
        if lowered == "rootbuilder" and os.path.isdir(os.path.join(plugins_dir, name)):
            return True
        if lowered.startswith("rootbuilder") and lowered.endswith(".py"):
            return True
    return False


def latest_asset(html: str) -> Optional[Tuple[str, str]]:
    """(download URL, version) of the newest rootbuilder.X.Y.Z.zip asset."""
    best: Optional[Tuple[Tuple[int, ...], str, str]] = None
    for match in _ASSET_RE.finditer(html or ""):
        version = tuple(int(p) for p in match.group(2).split("."))
        url = "https://github.com" + match.group(0)
        if best is None or version > best[0]:
            best = (version, url, match.group(2))
    return (best[1], best[2]) if best else None


def plugin_folders_in_zip(path: str) -> Tuple[str, List[str]]:
    """(prefix inside the zip, top-level folders to copy into plugins/).

    The prefix is whatever sits above the `rootbuilder/` package (some zips
    wrap it in `plugins/`); every folder at that level is copied, so shared
    helper folders shipped next to it come along."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    prefix = None
    for name in names:
        norm = name.replace("\\", "/")
        idx = norm.lower().find("rootbuilder/")
        if idx >= 0 and (idx == 0 or norm[idx - 1] == "/"):
            prefix = norm[:idx]
            break
    if prefix is None:
        raise ValueError("this zip does not contain a rootbuilder folder")
    tops = sorted({n.replace("\\", "/")[len(prefix):].split("/", 1)[0]
                   for n in names if n.replace("\\", "/").startswith(prefix)
                   and "/" in n.replace("\\", "/")[len(prefix):]})
    return prefix, tops


def install_zip(path: str, plugins_dir: str) -> List[str]:
    """Extract the plugin folders into plugins_dir. Returns folders written."""
    prefix, tops = plugin_folders_in_zip(path)
    base = os.path.realpath(plugins_dir)
    with zipfile.ZipFile(path) as zf:
        plan = []
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            rel = name[len(prefix):]
            if not rel or rel.split("/", 1)[0] not in tops:
                continue
            target = os.path.realpath(os.path.join(base, *rel.split("/")))
            if os.path.commonpath([target, base]) != base:   # no path traversal
                raise ValueError(f"unsafe path in zip: {name}")
            plan.append((info, target))
        # Every path validated before anything is written.
        for info, target in plan:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
    return tops


def mods_with_root(mods_path: str, names: Optional[Iterable[str]] = None) -> List[str]:
    """Mod folders that contain a top-level Root folder."""
    out: List[str] = []
    try:
        candidates = list(names) if names is not None else os.listdir(mods_path)
    except OSError:
        return out
    for name in candidates:
        folder = os.path.join(mods_path, name)
        try:
            if any(e.lower() == "root" and os.path.isdir(os.path.join(folder, e)) for e in os.listdir(folder)):
                out.append(name)
        except OSError:
            continue
    return out


def data_inside_root(mods_path: str, names: Iterable[str]) -> List[str]:
    """Mods Root Builder would ignore: a Data folder inside Root/."""
    bad = []
    for name in names:
        root = os.path.join(mods_path, name, "Root")
        try:
            if any(e.lower() == "data" for e in os.listdir(root)):
                bad.append(name)
        except OSError:
            continue
    return bad


# ---------------------------------------------------------------------------
# Game-folder ("engine injector") files
# ---------------------------------------------------------------------------

import shutil as _shutil

# DLL names the game loads from its own folder (proxy / injector DLLs).
INJECTOR_DLLS = {
    "dxgi.dll", "d3d9.dll", "d3d10.dll", "d3d11.dll", "d3d12.dll", "dinput8.dll", "version.dll",
    "winhttp.dll", "winmm.dll", "dsound.dll", "opengl32.dll", "xinput1_3.dll", "xinput1_4.dll",
    "xinput9_1_0.dll", "d3dx9_42.dll", "binkw64.dll", "tbb.dll", "tbbmalloc.dll",
    "d3dcompiler_46e.dll", "d3dcompiler_47.dll", "dbghelp.dll", "wininet.dll",
}
INJECTOR_FILES = {"enbseries.ini", "enblocal.ini", "enbadaptation.fx", "enbbloom.fx", "enbeffect.fx",
                  "enbeffectprepass.fx", "enbdepthoffield.fx", "enblens.fx", "enbsunsprite.fx",
                  "enbunderwater.fx", "reshade.ini", "dxvk.conf", "d3d11.ini", "dxgi.ini"}
INJECTOR_DIRS = {"enbseries", "enbcache", "reshade-shaders", "reshade-presets"}
_ROOT_FILE_RE = re.compile(r"(\.asi|_loader\.exe|^skse64_\d+_\d+_\d+\.dll|^sksevr_\d+_\d+_\d+\.dll)$", re.I)


def is_injector_entry(name: str, is_dir: bool) -> bool:
    lowered = name.lower()
    if is_dir:
        return lowered in INJECTOR_DIRS
    return lowered in INJECTOR_DLLS or lowered in INJECTOR_FILES or bool(_ROOT_FILE_RE.search(lowered))


def injector_entries(folder: str) -> List[str]:
    """Top-level entries of a Data-root folder that only work in the game folder."""
    out = []
    try:
        for name in os.listdir(folder):
            if is_injector_entry(name, os.path.isdir(os.path.join(folder, name))):
                out.append(name)
    except OSError:
        pass
    return sorted(out, key=str.lower)


def suspected_injector_mods(mods_path: str) -> List[Tuple[str, List[str]]]:
    """Installed mods with game-folder files sitting in their Data root."""
    out = []
    try:
        names = sorted(os.listdir(mods_path), key=str.lower)
    except OSError:
        return out
    for name in names:
        if name.endswith("_separator"):
            continue
        entries = injector_entries(os.path.join(mods_path, name))
        if entries:
            out.append((name, entries))
    return out


def _merge_move(src: str, dst: str) -> None:
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        for child in os.listdir(src):
            _merge_move(os.path.join(src, child), os.path.join(dst, child))
        os.rmdir(src)
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            os.remove(dst)
        os.replace(src, dst)


def move_to_root(mod_dir: str, everything: bool = False) -> List[str]:
    """Move game-folder files (or the whole mod) into mod_dir/Root.

    A top-level Data folder is never moved into Root (Root Builder would then
    ignore the mod); with everything=True its contents stay at the top level.
    Returns the moved top-level names."""
    root = os.path.join(mod_dir, "Root")
    moved = []
    for name in sorted(os.listdir(mod_dir), key=str.lower):
        lowered = name.lower()
        if lowered in ("meta.ini", "root"):
            continue
        path = os.path.join(mod_dir, name)
        if lowered == "data" and os.path.isdir(path):
            continue
        if everything or is_injector_entry(name, os.path.isdir(path)):
            _merge_move(path, os.path.join(root, name))
            moved.append(name)
    return moved


def move_from_root(mod_dir: str) -> List[str]:
    """Undo move_to_root: bring Root/'s contents back to the mod's top level."""
    root = os.path.join(mod_dir, "Root")
    if not os.path.isdir(root):
        return []
    moved = []
    for name in sorted(os.listdir(root), key=str.lower):
        _merge_move(os.path.join(root, name), os.path.join(mod_dir, name))
        moved.append(name)
    os.rmdir(root)
    return moved
