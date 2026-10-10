# -*- coding: utf-8 -*-
"""Wabbajack pre-compilation checks. Qt-free: the dialog gathers paths from
mobase and fetches URLs, then hands text/status codes to these functions."""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .common import (
    SEV_ERROR,
    SEV_INFO,
    SEV_OK,
    SEV_WARN,
    Finding,
    ProfileMod,
    free_bytes,
    human_size,
    image_size,
    is_inside,
    meta_get,
    norm,
    parse_version,
    read_ini,
    read_modlist,
    read_plugins,
    same_path,
    version_str,
)
from .model import MatchReport

REPOSITORIES_URL = (
    "https://raw.githubusercontent.com/wabbajack-tools/mod-lists/master/repositories.json"
)
STATUS_URL = (
    "https://raw.githubusercontent.com/wabbajack-tools/mod-lists/master/reports/"
    "{repo}/{machine}/status.json"
)
FEATURED_CRITERIA_URL = (
    "https://wiki.wabbajack.org/wabbajack_cdn_and_gallery_access/"
    "Criteria%20for%20'Featured%20Mod%20List'%20status.html"
)

# MO2 game short names -> Wabbajack Game enum names (compiler_settings "Game").
WJ_GAME_FOR_MO2 = {
    "skyrimse": "SkyrimSpecialEdition",
    "skyrim": "Skyrim",
    "skyrimvr": "SkyrimVR",
    "enderal": "Enderal",
    "enderalse": "EnderalSpecialEdition",
    "fallout4": "Fallout4",
    "fallout4vr": "Fallout4VR",
    "falloutnv": "FalloutNewVegas",
    "fallout3": "Fallout3",
    "oblivion": "Oblivion",
    "morrowind": "Morrowind",
    "starfield": "Starfield",
    "ttw": "FalloutNewVegas",
}

# The four Creations every Skyrim SE 1.6+ owner gets, plus the resource pack.
FREE_CC_STEMS = {
    "_resourcepack",
    "ccbgssse001-fish",
    "ccqdrsse001-survivalmode",
    "ccbgssse025-advdsgs",
    "ccbgssse037-curios",
}
CURIOS_GOOD = {"ccbgssse037-curios.esl", "ccbgssse037-curios.bsa"}
STOCK_GAME_NAMES = ("Stock Game", "Game Root", "Stock Folder", "Stock Game Folder")

_ISO_DATE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$"
)


@dataclass
class Context:
    """Everything the checks need from MO2, as plain strings."""

    base_path: str
    profiles_dir: str
    mods_path: str
    downloads_path: str
    overwrite_path: str
    game_dir: str
    data_dir: str
    game_short_name: str = ""
    game_names: Set[str] = field(default_factory=set)
    game_version: str = ""
    current_profile: str = ""
    plugin_data_dir: str = ""
    root_builder: Optional[bool] = None   # None = unknown


# ---------------------------------------------------------------------------
# compiler_settings discovery
# ---------------------------------------------------------------------------


def find_compiler_settings(base_path: str) -> List[str]:
    if not base_path or not os.path.isdir(base_path):
        return []
    files = glob.glob(os.path.join(glob.escape(base_path), "*.compiler_settings"))
    return sorted(files, key=lambda p: os.path.getmtime(p), reverse=True)


def pick_compiler_settings(paths: List[str], current_profile: str) -> str:
    for path in paths:
        try:
            if load_compiler_settings(path).get("Profile") == current_profile:
                return path
        except Exception:
            continue
    return paths[0] if paths else ""


def load_compiler_settings(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("top level is not a JSON object")
    return data


def _rel_list(settings: Dict[str, Any], key: str) -> List[str]:
    value = settings.get(key) or []
    return [str(v) for v in value if v] if isinstance(value, list) else []


def compiled_profiles(settings: Dict[str, Any]) -> List[str]:
    out = []
    if settings.get("Profile"):
        out.append(str(settings["Profile"]))
    out += [p for p in _rel_list(settings, "AdditionalProfiles") if p not in out]
    return out


def _covered(mod_name: str, prefixes: List[str]) -> bool:
    target = f"mods\\{mod_name}".lower()
    for prefix in prefixes:
        p = prefix.replace("/", "\\").rstrip("\\").lower()
        if target == p or target.startswith(p + "\\") or p.startswith(target + "\\"):
            return True
    return False


# ---------------------------------------------------------------------------
# Local checks against compiler_settings
# ---------------------------------------------------------------------------


def check_compiler_settings(
    ctx: Context, settings_path: str, settings: Dict[str, Any], report: Optional[MatchReport]
) -> List[Finding]:
    A = "Compiler settings"
    out: List[Finding] = [
        Finding(SEV_OK, A, f"Loaded {os.path.basename(settings_path)}", path=settings_path)
    ]

    # -- folders ---------------------------------------------------------
    source = str(settings.get("Source") or "")
    if not source:
        out.append(Finding(SEV_ERROR, A, "Source is empty", fix="Set the MO2 folder as Source in the compiler."))
    elif not os.path.isdir(source):
        out.append(Finding(SEV_ERROR, A, "Source folder does not exist", source, path=source))
    elif not same_path(source, ctx.base_path):
        out.append(Finding(
            SEV_WARN, A, "Source is not this MO2 instance",
            f"Source = {source}, MO2 base = {ctx.base_path}",
            "Compile from the instance you are validating, or open that instance in MO2.",
            path=source,
        ))
    else:
        out.append(Finding(SEV_OK, A, "Source matches this MO2 instance"))

    downloads = str(settings.get("Downloads") or "")
    if not downloads or not os.path.isdir(downloads):
        out.append(Finding(SEV_ERROR, A, "Downloads folder missing", downloads, path=downloads))
    elif not same_path(downloads, ctx.downloads_path):
        out.append(Finding(
            SEV_WARN, A, "Downloads differs from MO2's downloads folder",
            f"compiler: {downloads}\nMO2: {ctx.downloads_path}",
            "Wabbajack only matches archives in the compiler's Downloads folder.",
            path=downloads,
        ))
    else:
        out.append(Finding(SEV_OK, A, "Downloads matches MO2's downloads folder"))

    # -- game ------------------------------------------------------------
    game = str(settings.get("Game") or "")
    expected = WJ_GAME_FOR_MO2.get(ctx.game_short_name.lower(), "")
    if not game:
        out.append(Finding(SEV_ERROR, A, "Game is empty"))
    elif expected and game.lower() != expected.lower():
        out.append(Finding(
            SEV_ERROR, A, f"Game '{game}' does not match the managed game",
            f"MO2 manages {ctx.game_short_name}, which Wabbajack calls {expected}.",
        ))
    else:
        out.append(Finding(SEV_OK, A, f"Game: {game}"))
    for other in _rel_list(settings, "OtherGames"):
        out.append(Finding(SEV_INFO, A, f"OtherGames includes {other}",
                           "Users must own that game too, or installs fail."))

    # -- output ----------------------------------------------------------
    output = str(settings.get("OutputFile") or "")
    if not output:
        out.append(Finding(SEV_ERROR, A, "OutputFile is empty"))
    else:
        parent = os.path.dirname(output)
        if not output.lower().endswith(".wabbajack"):
            out.append(Finding(SEV_WARN, A, "OutputFile does not end in .wabbajack", output))
        if parent and not os.path.isdir(parent):
            out.append(Finding(SEV_ERROR, A, "OutputFile folder does not exist", parent, path=parent))
        if source and is_inside(output, source):
            out.append(Finding(
                SEV_WARN, A, "OutputFile is inside the Source folder",
                "The compiled file would sit inside the folder Wabbajack is indexing.",
                "Write the .wabbajack to a folder outside the MO2 instance.",
            ))
        free = free_bytes(parent or output)
        if free is not None:
            sev = SEV_WARN if free < 15 * 1024 ** 3 else SEV_INFO
            out.append(Finding(sev, A, f"Free space on output drive: {human_size(free)}",
                               "Large lists produce multi-GB .wabbajack files plus temp data.",
                               path=parent))

    # -- metadata --------------------------------------------------------
    for key in ("ModListName", "ModListAuthor", "ModListDescription"):
        if not str(settings.get(key) or "").strip():
            out.append(Finding(SEV_ERROR, A, f"{key} is empty"))
    version_text = str(settings.get("Version") or "")
    v = parse_version(version_text)
    if not version_text:
        legacy = settings.get("ModlistVersion")
        out.append(Finding(
            SEV_ERROR, A, "Version is not set",
            f"Only the obsolete ModlistVersion ({legacy}) is present." if legacy else "",
            "Set the list version in the compiler before publishing.",
        ))
    elif v is None:
        out.append(Finding(SEV_ERROR, A, f"Version '{version_text}' is not a valid version"))
    else:
        out.append(Finding(SEV_OK, A, f"Version: {version_text}"))
    if settings.get("ModlistVersion") and version_text:
        out.append(Finding(SEV_INFO, A, "ModlistVersion is obsolete and ignored",
                           f"Wabbajack uses Version ({version_text})."))

    image = str(settings.get("ModListImage") or "")
    if not image:
        out.append(Finding(SEV_WARN, A, "ModListImage is empty"))
    elif not os.path.isfile(image):
        out.append(Finding(SEV_ERROR, A, "ModListImage file does not exist", image, path=image))
    else:
        dims = image_size(image)
        if dims is None:
            out.append(Finding(SEV_WARN, A, "ModListImage is not a readable PNG/JPEG/WEBP", path=image))
        else:
            w, h, fmt = dims
            ratio = w / h if h else 0
            if abs(ratio - 16 / 9) > 0.05:
                out.append(Finding(SEV_INFO, A, f"ModListImage is {w}x{h} ({fmt})",
                                   "Gallery tiles are 16:9; other ratios get cropped or letterboxed.",
                                   path=image))
            else:
                out.append(Finding(SEV_OK, A, f"ModListImage {w}x{h} {fmt}"))

    machine = str(settings.get("MachineUrl") or "")
    if machine:
        parts = machine.split("/")
        if len(parts) != 2 or not all(parts) or " " in machine:
            out.append(Finding(SEV_ERROR, A, f"MachineUrl '{machine}' is malformed",
                               fix="Use <repositoryName>/<machineURL>, e.g. WhispersofMara/WhispersOfMara."))
    elif settings.get("PublishUpdate"):
        out.append(Finding(SEV_ERROR, A, "PublishUpdate is on but MachineUrl is empty"))

    if settings.get("UseTextureRecompression"):
        out.append(Finding(SEV_INFO, A, "Texture recompression is on",
                           "Compiles take longer; check that it is intended."))

    # -- profiles --------------------------------------------------------
    profiles = compiled_profiles(settings)
    if not profiles:
        out.append(Finding(SEV_ERROR, A, "No Profile set"))
    enabled_mods: Set[str] = set()
    for name in profiles:
        pdir = os.path.join(ctx.profiles_dir, name)
        if not os.path.isdir(pdir):
            out.append(Finding(SEV_ERROR, A, f"Profile '{name}' does not exist", path=pdir))
            continue
        mods = read_modlist(pdir)
        if not mods:
            out.append(Finding(SEV_ERROR, A, f"Profile '{name}' has no modlist.txt", path=pdir))
            continue
        on = [m for m in mods if m.enabled and not m.separator]
        enabled_mods.update(m.name for m in on)
        out.append(Finding(SEV_OK, A, f"Profile '{name}': {len(on)} enabled mods"))

    # -- path lists ------------------------------------------------------
    for key in ("NoMatchInclude", "Include", "Ignore", "AlwaysEnabled"):
        stale = []
        for rel in _rel_list(settings, key):
            full = os.path.join(source or ctx.base_path, rel.replace("\\", os.sep))
            if not os.path.exists(full):
                stale.append(rel)
        if stale:
            out.append(Finding(
                SEV_WARN, A, f"{len(stale)} {key} entr{'y' if len(stale) == 1 else 'ies'} point at nothing",
                "\n".join(stale[:40]),
                f"Remove the stale entries from {key}, or restore the folders.",
            ))
    both = set(p.lower() for p in _rel_list(settings, "Include")) & set(
        p.lower() for p in _rel_list(settings, "NoMatchInclude"))
    if both:
        out.append(Finding(SEV_INFO, A, "Paths listed in both Include and NoMatchInclude",
                           "\n".join(sorted(both))))

    covered = _rel_list(settings, "NoMatchInclude") + _rel_list(settings, "Include") + _rel_list(settings, "Ignore")

    if report is not None and enabled_mods:
        by_name = report.mods_by_name
        matched_names = {n for r in report.rows for n in r.mods}
        uncovered = sorted(
            n for n in enabled_mods
            if n in by_name and n not in matched_names and not _covered(n, covered)
        )
        if uncovered:
            out.append(Finding(
                SEV_WARN, "Mods", f"{len(uncovered)} enabled mod(s) have no archive in downloads",
                "Enabled in a compiled profile, no matching archive, and not covered by "
                "NoMatchInclude/Include. Their files will fail with 'no match' unless "
                "they already match another archive (e.g. generated or merged output):\n"
                + "\n".join(uncovered[:60]) + ("\n..." if len(uncovered) > 60 else ""),
                "Restore the archive (Download missing), or add the folder to NoMatchInclude.",
            ))
        else:
            out.append(Finding(SEV_OK, "Mods", "Every enabled mod has an archive or an include rule"))

        include_matched = [
            rel for rel in _rel_list(settings, "Include")
            if rel.lower().startswith("mods\\") and rel[5:] in matched_names
        ]
        if include_matched:
            out.append(Finding(
                SEV_INFO, A, "Include covers mods that also match a download",
                "Include inlines every file into the .wabbajack, so these make it larger "
                "than necessary:\n" + "\n".join(include_matched),
            ))
    return out


# ---------------------------------------------------------------------------
# Instance hygiene that does not need compiler_settings
# ---------------------------------------------------------------------------


def check_instance(ctx: Context, report: Optional[MatchReport]) -> List[Finding]:
    A = "Instance"
    out: List[Finding] = []

    # Root Builder: mods with a Root/ folder only reach the game through it.
    from . import rootbuilder as RB
    rooted = RB.mods_with_root(ctx.mods_path)
    if rooted and ctx.root_builder is False:
        out.append(Finding(
            SEV_WARN, "Root Builder", f"{len(rooted)} mod(s) have a Root folder but Root Builder is not installed",
            "Files in a mod's Root folder (SKSE loader, ENB, preloaders, tools) only reach the game "
            "folder through Root Builder:\n" + "\n".join(rooted[:40]),
            "Use 'Install Root Builder' below, or install it from Nexus.", url=RB.NEXUS_URL))
    elif rooted and ctx.root_builder:
        out.append(Finding(SEV_OK, "Root Builder", f"Root Builder installed; {len(rooted)} mod(s) use Root/"))
    suspects = RB.suspected_injector_mods(ctx.mods_path)
    if suspects:
        out.append(Finding(
            SEV_WARN, "Root Builder", f"{len(suspects)} mod(s) have game-folder files in their Data root",
            "Proxy DLLs, .asi plugins, loaders and ENB files do nothing in Data:\n"
            + "\n".join(f"{n}: {', '.join(e[:4])}" for n, e in suspects[:40]),
            "Use 'Move mods to Root…' to move them into Root/ for Root Builder."))
    bad_root = RB.data_inside_root(ctx.mods_path, rooted)
    if bad_root:
        out.append(Finding(
            SEV_WARN, "Root Builder", f"{len(bad_root)} mod(s) have a Data folder inside Root/",
            "Root Builder ignores these mods entirely:\n" + "\n".join(bad_root[:40]),
            "Move Root/Data's contents to the mod's top level.", url=RB.DOCS_URL))

    # overwrite
    count = 0
    if ctx.overwrite_path and os.path.isdir(ctx.overwrite_path):
        for _root, _dirs, files in os.walk(ctx.overwrite_path):
            count += len(files)
            if count > 500:
                break
    if count:
        out.append(Finding(
            SEV_WARN, A, f"Overwrite is not empty ({count}{'+' if count > 500 else ''} files)",
            "Wabbajack ships overwrite as-is; tool output and logs left there end up in every install.",
            "Move wanted files into a mod and clear the rest.",
            path=ctx.overwrite_path,
        ))
    else:
        out.append(Finding(SEV_OK, A, "Overwrite is empty"))

    # executables pointing outside the instance / game folder
    mo2_ini = os.path.join(ctx.base_path, "ModOrganizer.ini")
    ini = read_ini(mo2_ini)
    execs = ini.get("customexecutables", {})
    outside = []
    for key, value in execs.items():
        if not key.endswith("\\binary") and not key.endswith("/binary"):
            continue
        path = value.strip()
        if path.startswith("@ByteArray(") and path.endswith(")"):
            path = path[len("@ByteArray("):-1]
        if not path or not (os.path.isabs(path) or re.match(r"^[A-Za-z]:[\\/]", path)):
            continue
        if is_inside(path, ctx.base_path) or is_inside(path, ctx.game_dir):
            continue
        title = execs.get(key.rsplit("\\", 1)[0] + "\\title", "") or os.path.basename(path)
        outside.append(f"{title}: {value}")
    if outside:
        out.append(Finding(
            SEV_WARN, A, f"{len(outside)} executable(s) point outside the instance",
            "Wabbajack can only remap paths inside the MO2 folder or game folder; "
            "these will be broken on users' machines:\n" + "\n".join(outside),
            "Move the tools into the instance (e.g. a 'tools' folder) and re-point them.",
            path=mo2_ini,
        ))
    elif execs:
        out.append(Finding(SEV_OK, A, "All executables live inside the instance or game folder"))

    if report is None:
        return out

    A = "Downloads"
    no_meta = [r.download.archive for r in report.rows if r.download.has_archive and not r.download.has_meta]
    if no_meta:
        out.append(Finding(
            SEV_WARN, A, f"{len(no_meta)} archive(s) have no .meta",
            "Wabbajack cannot tell where these came from, so any mod built from them "
            "fails to source:\n" + "\n".join(no_meta[:40]),
            "Right-click in MO2's Downloads tab > Query Info, or add a .meta with a "
            "directURL/manualURL.",
            path=ctx.downloads_path,
        ))
    no_source = [
        r.download.archive for r in report.rows
        if r.download.has_meta and r.download.has_archive and not r.download.has_source
    ]
    if no_source:
        out.append(Finding(
            SEV_WARN, A, f"{len(no_source)} .meta file(s) have no usable source",
            "No Nexus mod/file ID and no directURL/manualURL:\n" + "\n".join(no_source[:40]),
            "Query Info for Nexus files; add directURL or manualURL for the rest.",
            path=ctx.downloads_path,
        ))
    orphans = [r.download.archive for r in report.rows if r.download.has_meta and not r.download.has_archive]
    if orphans:
        out.append(Finding(SEV_INFO, A, f"{len(orphans)} .meta file(s) without an archive",
                           "\n".join(orphans[:40]), path=ctx.downloads_path))
    unflagged = [r for r in report.matched_rows if r.download.has_meta and not r.download.installed_flag]
    if unflagged:
        out.append(Finding(
            SEV_INFO, A, f"{len(unflagged)} matched download(s) not flagged installed",
            "Use the Download matching tab to mark them.",
        ))
    return out


# ---------------------------------------------------------------------------
# Stock Game / Creation Club
# ---------------------------------------------------------------------------


def _dll_runtime(name: str) -> Optional[Tuple[int, int, int]]:
    match = re.fullmatch(r"skse64_(\d+)_(\d+)_(\d+)\.dll", name, re.I)
    return tuple(int(x) for x in match.groups()) if match else None  # type: ignore


def check_stock_game(ctx: Context, settings: Optional[Dict[str, Any]]) -> List[Finding]:
    A = "Stock Game"
    out: List[Finding] = []
    game_dir = ctx.game_dir
    if not game_dir or not os.path.isdir(game_dir):
        return [Finding(SEV_ERROR, A, "MO2's game folder does not exist", game_dir, path=game_dir)]

    if is_inside(game_dir, ctx.base_path):
        out.append(Finding(SEV_OK, A, f"Stock Game in use: {os.path.relpath(game_dir, ctx.base_path)}",
                           path=game_dir))
    else:
        candidates = [
            os.path.join(ctx.base_path, n) for n in STOCK_GAME_NAMES
            if os.path.isdir(os.path.join(ctx.base_path, n))
        ]
        out.append(Finding(
            SEV_WARN, A, "MO2 points at the installed game, not a Stock Game folder",
            f"Game folder: {game_dir}\nWabbajack's featured-list criteria expect lists not to "
            "require downgrading or modifying the user's own game install; Stock Game "
            "handles both." + (f"\nFound a likely Stock Game folder: {candidates[0]}" if candidates else ""),
            "Copy the game into the instance (e.g. 'Stock Game') and point MO2's "
            "Instance > Game folder at it.",
            path=game_dir, url=FEATURED_CRITERIA_URL,
        ))

    if ctx.game_short_name.lower() not in ("skyrimse", "skyrimvr", "enderalse"):
        return out

    data_dir = ctx.data_dir or os.path.join(game_dir, "Data")
    try:
        data_files = os.listdir(data_dir)
    except OSError:
        out.append(Finding(SEV_ERROR, A, "Game Data folder is unreadable", data_dir, path=data_dir))
        return out

    # -- Rare Curios ------------------------------------------------------
    curios = [f for f in data_files if f.lower() in CURIOS_GOOD]
    wrong_case = [f for f in curios if f not in CURIOS_GOOD]
    if not curios and ctx.game_short_name.lower() == "skyrimse":
        out.append(Finding(SEV_INFO, A, "Rare Curios is not in the game Data folder",
                           "Fine if the list does not rely on it; most SE 1.6 lists do."))
    elif wrong_case:
        out.append(Finding(
            SEV_ERROR, "Creation Club", "Rare Curios is the Steam-shipped (upper-case) version",
            "Found: " + ", ".join(sorted(wrong_case)) + ". Steam's copy hashes differently from "
            "the one downloaded through the in-game Creations menu; Wabbajack requires featured "
            "Skyrim lists to use the in-game lower-case curios file.",
            "Delete both ccBGSSSE037-Curios files from the Steam Data folder, launch Skyrim, "
            "download Rare Curios from Creations, then copy the lower-case "
            "ccbgssse037-curios.esl/.bsa into the Stock Game Data folder.",
            path=data_dir, url=FEATURED_CRITERIA_URL,
        ))
    else:
        out.append(Finding(SEV_OK, "Creation Club", "Rare Curios is the in-game (lower-case) Creations version"))

    # Curios packed into a mod is the same problem one folder over.
    mod_curios = []
    if ctx.mods_path and os.path.isdir(ctx.mods_path):
        for mod in os.listdir(ctx.mods_path):
            mpath = os.path.join(ctx.mods_path, mod)
            try:
                for f in os.listdir(mpath):
                    if f.lower() in CURIOS_GOOD and f not in CURIOS_GOOD:
                        mod_curios.append(f"{mod}\\{f}")
            except OSError:
                continue
    if mod_curios:
        out.append(Finding(
            SEV_ERROR, "Creation Club", "Upper-case Curios files installed as a mod",
            "\n".join(mod_curios[:20]),
            "Replace them with the in-game Creations (lower-case) files.",
            path=ctx.mods_path, url=FEATURED_CRITERIA_URL,
        ))

    # -- Creation Club beyond the free four -------------------------------
    stems_present = {os.path.splitext(f)[0].lower() for f in data_files}
    missing_free = sorted(s for s in FREE_CC_STEMS if s not in stems_present)
    if missing_free and ctx.game_short_name.lower() == "skyrimse":
        out.append(Finding(SEV_INFO, "Creation Club", "Free Creations missing from Data",
                           ", ".join(missing_free)))
    paid = sorted({
        f for f in data_files
        if f.lower().startswith("cc") and f.lower().endswith((".esm", ".esl", ".esp"))
        and os.path.splitext(f)[0].lower() not in FREE_CC_STEMS
    })
    if paid:
        enabled = set()
        if settings:
            for pname in compiled_profiles(settings):
                for plugin, on in read_plugins(os.path.join(ctx.profiles_dir, pname)):
                    if on:
                        enabled.add(plugin.lower())
        used = [p for p in paid if p.lower() in enabled]
        out.append(Finding(
            SEV_WARN if used else SEV_INFO, "Creation Club",
            f"{len(paid)} paid (Anniversary) Creation plugin(s) in the game Data folder"
            + (f", {len(used)} enabled in compiled profiles" if used else ""),
            ("Enabled: " + ", ".join(used[:30]) + "\n" if used else "")
            + "Users without the Anniversary Upgrade cannot supply these files. Skyrim also "
            "loads anything listed in Skyrim.ccc that is present, whatever plugins.txt says.",
            "Either state 'Anniversary Upgrade required' in the readme/description, or remove "
            "paid Creations from the Stock Game Data folder.",
            path=data_dir,
        ))

    # -- SKSE vs game runtime ---------------------------------------------
    game_v = parse_version(ctx.game_version)
    dlls = [f for f in os.listdir(game_dir) if _dll_runtime(f)]
    loader = any(f.lower() == "skse64_loader.exe" for f in os.listdir(game_dir))
    if game_v:
        out.append(Finding(SEV_INFO, A, f"Game runtime {version_str(game_v)}"))
    if dlls:
        runtimes = {_dll_runtime(f) for f in dlls}
        if game_v and tuple(game_v[:3]) not in runtimes:
            out.append(Finding(
                SEV_ERROR, A, "SKSE does not match the game runtime",
                f"Game {version_str(game_v)}; SKSE DLL(s): {', '.join(dlls)}",
                "Install the SKSE build for this runtime into the Stock Game folder.",
                path=game_dir,
            ))
        else:
            out.append(Finding(SEV_OK, A, f"SKSE present ({', '.join(dlls)})"))
        if not loader:
            out.append(Finding(SEV_WARN, A, "skse64_loader.exe is missing", path=game_dir))
    elif ctx.game_short_name.lower() == "skyrimse":
        out.append(Finding(SEV_INFO, A, "No SKSE found in the game folder",
                           "Fine if SKSE is deployed via Root Builder or not used."))
    return out


# ---------------------------------------------------------------------------
# Online: repositories.json -> modlists.json -> links -> status report
# ---------------------------------------------------------------------------


def split_machine_url(settings: Dict[str, Any]) -> Tuple[str, str]:
    machine = str(settings.get("MachineUrl") or "")
    if machine.count("/") != 1:
        return "", ""
    repo, name = machine.split("/")
    return repo, name


def check_repositories(settings: Dict[str, Any], text: Optional[str], code: Optional[int]) -> Tuple[List[Finding], str, str]:
    """Returns (findings, modlists.json URL, repository key as listed)."""
    A = "Repository"
    repo, _machine = split_machine_url(settings)
    if not repo:
        return [Finding(SEV_INFO, A, "No MachineUrl, skipping repository checks")], "", ""
    if text is None or code != 200:
        return [Finding(SEV_WARN, A, f"Could not fetch repositories.json (HTTP {code})", url=REPOSITORIES_URL)], "", ""
    try:
        repos = json.loads(text)
    except ValueError as exc:
        return [Finding(SEV_WARN, A, f"repositories.json did not parse: {exc}")], "", ""
    if repo in repos:
        return [Finding(SEV_OK, A, f"Repository '{repo}' is registered with Wabbajack",
                        url=repos[repo])], repos[repo], repo
    ci = {k.lower(): k for k in repos}
    if repo.lower() in ci:
        real = ci[repo.lower()]
        return [Finding(
            SEV_ERROR, A, f"Repository name case differs: '{repo}' vs registered '{real}'",
            fix=f"Use MachineUrl '{real}/{_machine}'.", url=REPOSITORIES_URL,
        )], repos[real], real
    return [Finding(
        SEV_ERROR, A, f"Repository '{repo}' is not in Wabbajack's repositories.json",
        "Wabbajack cannot find the list until the repository is registered.",
        "Open a PR to wabbajack-tools/mod-lists adding your repo, or fix the MachineUrl.",
        url=REPOSITORIES_URL,
    )], "", ""


def check_modlists_json(
    settings: Dict[str, Any],
    text: Optional[str],
    code: Optional[int],
    url: str,
    repo_key: str,
    output_free: Optional[int],
) -> Tuple[List[Finding], Optional[Dict[str, Any]]]:
    A = "Repository"
    _repo, machine = split_machine_url(settings)
    if text is None or code != 200:
        return [Finding(SEV_ERROR, A, f"modlists.json unreachable (HTTP {code})", url=url)], None
    try:
        lists = json.loads(text)
    except ValueError as exc:
        return [Finding(
            SEV_ERROR, A, "modlists.json is not valid JSON",
            str(exc), "Wabbajack drops the whole repository when this file fails to parse.", url=url,
        )], None
    if not isinstance(lists, list):
        return [Finding(SEV_ERROR, A, "modlists.json must be a JSON array", url=url)], None

    out: List[Finding] = [Finding(SEV_OK, A, f"modlists.json parses ({len(lists)} list(s))", url=url)]

    machines = [((e.get("links") or {}).get("machineURL") or "") for e in lists if isinstance(e, dict)]
    dupes = sorted({m for m in machines if m and machines.count(m) > 1})
    if dupes:
        out.append(Finding(SEV_ERROR, A, "Duplicate machineURL values", ", ".join(dupes), url=url))

    entry = next((e for e in lists if isinstance(e, dict) and (e.get("links") or {}).get("machineURL") == machine), None)
    if entry is None:
        ci = next((e for e in lists if isinstance(e, dict)
                   and str((e.get("links") or {}).get("machineURL") or "").lower() == machine.lower()), None)
        if ci is not None:
            out.append(Finding(SEV_ERROR, A, "machineURL case differs from MachineUrl",
                               f"modlists.json: {ci['links']['machineURL']}, compiler: {machine}", url=url))
            entry = ci
        else:
            out.append(Finding(SEV_ERROR, A, f"No entry with machineURL '{machine}'",
                               f"Present: {', '.join(m for m in machines if m) or 'none'}", url=url))
            return out, None

    links = entry.get("links") or {}
    for key in ("title", "author", "description", "game"):
        if not str(entry.get(key) or "").strip():
            out.append(Finding(SEV_ERROR, A, f"Entry is missing '{key}'", url=url))
    for key in ("image", "readme", "download", "machineURL"):
        if not str(links.get(key) or "").strip():
            out.append(Finding(SEV_ERROR, A, f"Entry is missing links.{key}", url=url))

    game = str(settings.get("Game") or "")
    if entry.get("game") and game and str(entry["game"]).lower() != game.lower():
        out.append(Finding(SEV_ERROR, A, f"game '{entry['game']}' does not match compiler Game '{game}'", url=url))
    if "nsfw" in entry and bool(entry.get("nsfw")) != bool(settings.get("ModlistIsNSFW")):
        out.append(Finding(SEV_WARN, A, "nsfw flag differs between modlists.json and the compiler",
                           f"modlists.json nsfw={entry.get('nsfw')}, ModlistIsNSFW={settings.get('ModlistIsNSFW')}",
                           url=url))
    if entry.get("utility_list"):
        out.append(Finding(
            SEV_WARN, A, "utility_list is true",
            "The gallery treats utility lists as tools/dev setups and keeps them out of the "
            "normal list view, so players browsing normally will not see it.",
            "Set \"utility_list\": false for a playable modlist.", url=url,
        ))
    if entry.get("force_down"):
        out.append(Finding(SEV_ERROR, A, "force_down is true",
                           "The gallery shows the list as down.", url=url))
    if repo_key and entry.get("repositoryName") and entry["repositoryName"] != repo_key:
        out.append(Finding(SEV_WARN, A, f"repositoryName '{entry['repositoryName']}' differs from '{repo_key}'", url=url))

    for key in ("dateCreated", "dateUpdated"):
        value = entry.get(key)
        if value and not _ISO_DATE.match(str(value)):
            out.append(Finding(
                SEV_ERROR, A, f"{key} is not a valid ISO date: '{value}'",
                "Wabbajack reads this field as a date; a malformed value can stop it reading "
                "the repository file, which hides every list in it.",
                f"Use the form 2026-09-21T17:38:00Z.", url=url,
            ))

    repo_v = parse_version(str(entry.get("version") or ""))
    local_v = parse_version(str(settings.get("Version") or ""))
    if repo_v and local_v:
        if local_v <= repo_v:
            sev = SEV_ERROR if settings.get("PublishUpdate") else SEV_WARN
            out.append(Finding(
                sev, A, f"Compiler Version {version_str(local_v)} is not newer than published {version_str(repo_v)}",
                fix="Bump Version before compiling an update.", url=url,
            ))
        else:
            out.append(Finding(SEV_OK, A, f"Version {version_str(local_v)} > published {version_str(repo_v)}"))

    if str(entry.get("title") or "") and settings.get("ModListName") and entry["title"] != settings["ModListName"]:
        out.append(Finding(SEV_INFO, A, "Gallery title differs from ModListName",
                           f"'{entry['title']}' vs '{settings['ModListName']}'"))

    download = str(links.get("download") or "")
    if download and not download.startswith("https://authored-files.wabbajack.org/"):
        out.append(Finding(SEV_INFO, A, "links.download is not on authored-files.wabbajack.org", download))

    meta = entry.get("download_metadata") or {}
    if not meta:
        out.append(Finding(SEV_WARN, A, "download_metadata missing",
                           "Wabbajack uses it for sizes and the update check."))
    else:
        size = meta.get("Size")
        if isinstance(size, (int, float)) and output_free is not None and output_free < size * 2:
            out.append(Finding(
                SEV_WARN, "Compiler settings", "Output drive is tight for this list",
                f"Last .wabbajack was {human_size(int(size))}; {human_size(output_free)} free.",
            ))
    return out, entry


def link_targets(settings: Dict[str, Any], entry: Optional[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """(label, url) pairs worth an HTTP check."""
    targets: List[Tuple[str, str]] = []
    for key, label in (("ModListReadme", "Readme (compiler)"), ("ModListWebsite", "Website"),
                       ("ModListCommunity", "Community")):
        value = str(settings.get(key) or "").strip()
        if value:
            targets.append((label, value))
    if entry:
        links = entry.get("links") or {}
        for key in ("image", "readme", "discordURL"):
            value = str(links.get(key) or "").strip()
            if value and value not in {u for _l, u in targets}:
                targets.append((f"modlists.json links.{key}", value))
    return targets


def discord_probe_url(url: str) -> str:
    """discord.gg serves a 200 page for dead invites; the API does not."""
    match = re.match(r"^https?://(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([\w-]+)", url, re.I)
    return f"https://discord.com/api/v10/invites/{match.group(1)}" if match else ""


def link_finding(label: str, url: str, code: Optional[int], content_type: str) -> Finding:
    A = "Links"
    if code is None:
        return Finding(SEV_WARN, A, f"{label}: no response", url=url)
    if discord_probe_url(url):
        if code == 404:
            return Finding(SEV_ERROR, A, f"{label}: Discord invite is invalid or expired",
                           fix="Create a non-expiring invite.", url=url)
        if code == 200:
            return Finding(SEV_OK, A, f"{label}: Discord invite is valid", url=url)
    if 200 <= code < 400:
        if "image" in label and content_type and not content_type.startswith("image/"):
            return Finding(SEV_WARN, A, f"{label}: served as {content_type}, not an image", url=url)
        return Finding(SEV_OK, A, f"{label}: HTTP {code}", url=url)
    sev = SEV_ERROR if code in (404, 410) else SEV_WARN
    return Finding(sev, A, f"{label}: HTTP {code}", url=url)


def check_status_report(
    text: Optional[str], code: Optional[int], url: str, downloads: Set[str]
) -> List[Finding]:
    A = "Published list"
    if code == 404:
        return [Finding(SEV_INFO, A, "No Wabbajack validation report found for this list", url=url)]
    if text is None or code != 200:
        return [Finding(SEV_WARN, A, f"Validation report unreachable (HTTP {code})", url=url)]
    try:
        report = json.loads(text)
    except ValueError as exc:
        return [Finding(SEV_WARN, A, f"Validation report did not parse: {exc}", url=url)]
    archives = report.get("Archives") or []
    statuses: Dict[str, int] = {}
    bad: List[str] = []
    for item in archives:
        status = str(item.get("Status") or "?")
        statuses[status] = statuses.get(status, 0) + 1
        if status.lower() not in ("valid", "mirrored"):
            original = item.get("Original") or {}
            name = str(original.get("Name") or "?")
            state = original.get("State") or {}
            where = ""
            if state.get("ModID"):
                where = f" (Nexus {state.get('ModID')}/{state.get('FileID')})"
            local = " - you still have this archive" if name in downloads else ""
            bad.append(f"{status}: {name}{where}{local}")
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(statuses.items()))
    out = [Finding(SEV_OK if not bad else SEV_INFO, A,
                   f"Wabbajack validated {len(archives)} archive(s) for {report.get('Name') or 'this list'} "
                   f"v{report.get('Version') or '?'}", summary, url=url)]
    if bad:
        out.append(Finding(
            SEV_ERROR, A, f"{len(bad)} archive(s) failing Wabbajack's validation",
            "\n".join(bad[:60]),
            "Replace or re-host these before the next release; users installing the published "
            "version will hit them.",
            url=url,
        ))
    return out
