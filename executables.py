# -*- coding: utf-8 -*-
"""Find programs inside installed mods (SKSE, BodySlide, Pandora, xEdit...)
and register them as MO2 executables.

MO2 keeps its executables list in memory and writes it back to
ModOrganizer.ini before every program launch and when it closes, so editing
the ini while MO2 runs is silently undone, and the Python API has no
add-executable call. The merge is therefore applied by a small detached
PowerShell helper that waits for MO2 to exit, then adds only entries whose
title is not already present.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

# (pattern on file name, title, lives in Root/ i.e. runs from the game folder, show in toolbar)
KNOWN_TOOLS: List[Tuple[str, str, bool, bool]] = [
    (r"skse64_loader\.exe", "SKSE", True, True),
    (r"sksevr_loader\.exe", "SKSE VR", True, True),
    (r"bodyslide x64\.exe", "BodySlide", False, False),
    (r"outfitstudio x64\.exe", "Outfit Studio", False, False),
    (r"pandora behaviour engine\+?\.exe", "Pandora Behaviour Engine", False, False),
    (r"nemesis unlimited behavior engine\.exe", "Nemesis", False, False),
    (r"generatefnisforusers\.exe", "FNIS", False, False),
    (r"(sseedit|sseedit64|xedit|xedit64)\.exe", "SSEEdit", False, False),
    (r"sseeditquickautoclean\.exe", "SSEEdit QuickAutoClean", False, False),
    (r"dyndolodx64\.exe", "DynDOLOD", False, False),
    (r"texgenx64\.exe", "TexGen", False, False),
    (r"xlodgenx64\.exe", "xLODGen", False, False),
    (r"loot\.exe", "LOOT", False, False),
    (r"synthesis\.exe", "Synthesis", False, False),
    (r"(pgpatcher|parallaxgen)\.exe", "PGPatcher", False, False),
    (r"skyrimruntimeswapper\.exe", "Skyrim Runtime Swapper", True, False),
]
_KNOWN = [(re.compile(rf"^{p}$", re.I), t, r, tb) for p, t, r, tb in KNOWN_TOOLS]
SKIP_DIRS = {"textures", "meshes", "sound", "music", "scripts", "seq", "strings", "interface", "video"}


@dataclass
class Tool:
    title: str
    binary: str
    working_dir: str = ""
    arguments: str = ""
    toolbar: bool = False
    source: str = ""          # mod name / "game folder" / "curator"
    needs_root_builder: bool = False


def _find_exes(folder: str, max_depth: int = 4) -> Iterable[Tuple[str, str]]:
    """(absolute path, path relative to folder) of every .exe, skipping
    asset folders that never hold programs."""
    base_depth = folder.rstrip("\\/").count(os.sep)
    for dirpath, dirs, files in os.walk(folder):
        depth = dirpath.count(os.sep) - base_depth
        if depth == 0:
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
        if depth >= max_depth:
            dirs[:] = []
        for name in files:
            if name.lower().endswith(".exe"):
                full = os.path.join(dirpath, name)
                yield full, os.path.relpath(full, folder)


def detect_tools(mods_path: str, mod_folders: Iterable[str], game_dir: str,
                 curator_tools: Optional[List[Dict[str, str]]] = None) -> List[Tool]:
    tools: List[Tool] = []
    seen: Set[str] = set()

    def add(tool: Tool) -> None:
        key = os.path.normcase(os.path.normpath(tool.binary))
        if key not in seen:
            seen.add(key)
            tools.append(tool)

    folders = list(mod_folders)
    # Curator tools first so their titles win (paths are relative to the game folder).
    for ct in curator_tools or []:
        rel = re.sub(r"^data[\\/]", "", str(ct.get("exe") or ""), flags=re.I)
        if not rel:
            continue
        for folder in folders:
            candidate = os.path.join(mods_path, folder, *re.split(r"[\\/]+", rel))
            if os.path.isfile(candidate):
                add(Tool(str(ct.get("name") or os.path.basename(rel)), candidate,
                         os.path.dirname(candidate), " ".join(ct.get("args") or []), False, folder))
                break

    for folder in folders:
        root = os.path.join(mods_path, folder)
        for full, rel in _find_exes(root):
            name = os.path.basename(rel)
            for pattern, title, in_root, toolbar in _KNOWN:
                if not pattern.match(name):
                    continue
                parts = re.split(r"[\\/]+", rel)
                if parts[0].lower() == "root":
                    # Root Builder places it in the game folder at launch.
                    binary = os.path.join(game_dir, *parts[1:])
                    add(Tool(title, binary, os.path.dirname(binary), "", toolbar, folder, True))
                elif not in_root:
                    add(Tool(title, full, os.path.dirname(full), "", toolbar, folder))
                break

    for pattern, title, in_root, toolbar in _KNOWN:
        if not in_root:
            continue
        try:
            for name in os.listdir(game_dir):
                if pattern.match(name):
                    add(Tool(title, os.path.join(game_dir, name), game_dir, "", toolbar, "game folder"))
        except OSError:
            pass
    return tools


# ---------------------------------------------------------------------------
# ModOrganizer.ini [customExecutables]
# ---------------------------------------------------------------------------

_SECTION = "customExecutables"


def existing_titles(ini_text: str) -> Set[str]:
    titles: Set[str] = set()
    in_section = False
    for line in ini_text.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_section = s[1:-1].strip().lower() == _SECTION.lower()
            continue
        if in_section:
            m = re.match(r"^\d+\\title=(.*)$", s)
            if m:
                titles.add(_unquote(m.group(1)).lower())
    return titles


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def qsettings_value(value: str) -> str:
    """Encode a string the way QSettings' IniFormat would read it back."""
    value = str(value)
    if value == "" or not re.search(r'[,;="\\]|^\s|\s$', value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def entry_lines(index: int, tool: Tool) -> List[str]:
    fwd = lambda p: p.replace("\\", "/")
    values = [
        ("title", tool.title),
        ("binary", fwd(tool.binary)),
        ("arguments", tool.arguments),
        ("workingDirectory", fwd(tool.working_dir)),
        ("steamAppID", ""),
        ("ownicon", "true"),
        ("toolbar", "true" if tool.toolbar else "false"),
        ("hide", "false"),
        ("minimizeToSystemTray", "false"),
    ]
    return [f"{index}\\{k}={qsettings_value(v)}" for k, v in values]


def merge_ini(ini_text: str, tools: List[Tool]) -> Tuple[str, List[str]]:
    """Return (new ini text, titles added). Mirrors the PowerShell helper."""
    newline = "\r\n" if "\r\n" in ini_text else "\n"
    lines = ini_text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip().lower() == f"[{_SECTION.lower()}]"), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{_SECTION}]", "size=0"]
        start = len(lines) - 2
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
    titles = existing_titles(newline.join(lines))
    size = 0
    size_line = None
    title_idx: Dict[str, str] = {}
    key_line: Dict[Tuple[str, str], int] = {}
    for i in range(start + 1, end):
        m = re.match(r"^size=(\d+)\s*$", lines[i].strip())
        if m:
            size, size_line = int(m.group(1)), i
        m = re.match(r"^(\d+)\\(\w+)=(.*)$", lines[i].strip())
        if m:
            size = max(size, int(m.group(1)))
            key_line[(m.group(1), m.group(2).lower())] = i
            if m.group(2).lower() == "title":
                title_idx[_unquote(m.group(3)).lower()] = m.group(1)
    added: List[str] = []
    new: List[str] = []
    for tool in tools:
        if tool.title.lower() in titles:
            # Repoint an existing entry only when its program no longer exists.
            idx = title_idx.get(tool.title.lower())
            bin_line = key_line.get((idx, "binary")) if idx else None
            current = _unquote(lines[bin_line].split("=", 1)[1]) if bin_line is not None else ""
            if idx and not os.path.exists(current):
                fresh = dict(l.split("=", 1) for l in entry_lines(int(idx), tool))
                for key in ("binary", "workingDirectory"):
                    li = key_line.get((idx, key.lower()))
                    text = f"{idx}\\{key}={fresh[f'{idx}\\{key}']}"
                    if li is not None:
                        lines[li] = text
                    else:
                        lines.insert(bin_line + 1, text)
                added.append(tool.title + " (repointed)")
            continue
        size += 1
        new += entry_lines(size, tool)
        titles.add(tool.title.lower())
        added.append(tool.title)
    if not new and not added:
        return ini_text, added
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
    insert_at = end
    while insert_at - 1 > start and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines[insert_at:insert_at] = new
    if size_line is not None:
        lines[size_line] = f"size={size}"
    elif new:
        lines.insert(insert_at + len(new), f"size={size}")
    out = newline.join(lines)
    if ini_text.endswith(("\n", "\r\n")):
        out += newline
    return out, added


# The helper re-implements merge_ini; it reads the ini only after MO2 has
# exited and written its own final copy.
PS_SCRIPT = r'''param([int]$MoPid, [string]$Ini, [string]$Entries, [string]$Log)
$ErrorActionPreference = "Stop"
function Say($m) { Add-Content -LiteralPath $Log -Value ("[{0}] {1}" -f (Get-Date -Format s), $m) }
try {
  Say "waiting for Mod Organizer (pid $MoPid) to close"
  try { Wait-Process -Id $MoPid -ErrorAction Stop } catch { }
  Start-Sleep -Seconds 2
  $enc = New-Object System.Text.UTF8Encoding($false)
  $text = [IO.File]::ReadAllText($Ini, $enc)
  $nl = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
  $lines = New-Object System.Collections.Generic.List[string]
  $lines.AddRange([string[]]($text -split "\r?\n"))
  if ($lines.Count -gt 0 -and $lines[$lines.Count-1] -eq "") { $lines.RemoveAt($lines.Count-1) }
  $start = -1
  for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].Trim() -ieq "[customExecutables]") { $start = $i; break } }
  if ($start -lt 0) { $lines.Add(""); $lines.Add("[customExecutables]"); $lines.Add("size=0"); $start = $lines.Count - 2 }
  $end = $lines.Count
  for ($i = $start + 1; $i -lt $lines.Count; $i++) { if ($lines[$i].Trim().StartsWith("[")) { $end = $i; break } }
  $titles = @{}; $titleIdx = @{}; $keyLine = @{}
  $size = 0; $sizeLine = -1
  for ($i = $start + 1; $i -lt $end; $i++) {
    $l = $lines[$i].Trim()
    if ($l -match '^size=(\d+)$') { $size = [int]$Matches[1]; $sizeLine = $i }
    if ($l -match '^(\d+)\\(\w+)=(.*)$') {
      $idx = $Matches[1]; $key = $Matches[2].ToLower(); $val = $Matches[3]
      if ([int]$idx -gt $size) { $size = [int]$idx }
      $keyLine["$idx|$key"] = $i
      if ($key -eq "title") { $t = $val.Trim().Trim('"').ToLower(); $titles[$t] = $true; $titleIdx[$t] = $idx }
    }
  }
  $changed = $false
  $entries = Get-Content -LiteralPath $Entries -Raw -Encoding UTF8 | ConvertFrom-Json
  $new = New-Object System.Collections.Generic.List[string]
  $added = @()
  foreach ($e in $entries) {
    $tl = $e.title.ToLower()
    if ($titles.ContainsKey($tl)) {
      $idx = $titleIdx[$tl]; $bl = $keyLine["$idx|binary"]
      $cur = if ($bl -ne $null) { ($lines[$bl] -split '=', 2)[1].Trim().Trim('"') } else { "" }
      if ($cur -and (Test-Path -LiteralPath $cur)) { Say "skip (exists): $($e.title)"; continue }
      foreach ($key in @("binary", "workingDirectory")) {
        $text = "$idx\$key=" + $e.values.$key
        $li = $keyLine["$idx|$($key.ToLower())"]
        if ($li -ne $null) { $lines[$li] = $text } elseif ($bl -ne $null) { $lines.Insert($bl + 1, $text) }
      }
      $changed = $true; $added += "$($e.title) (repointed)"; continue
    }
    $size++
    foreach ($kv in $e.lines) { $new.Add(($kv -replace '^#', "$size")) }
    $titles[$e.title.ToLower()] = $true
    $added += $e.title
  }
  if ($added.Count -eq 0) { Say "nothing to add"; exit 0 }
  $end = $lines.Count
  for ($i = $start + 1; $i -lt $lines.Count; $i++) { if ($lines[$i].Trim().StartsWith("[")) { $end = $i; break } }
  $insert = $end
  while ($insert - 1 -gt $start -and $lines[$insert-1].Trim() -eq "") { $insert-- }
  $lines.InsertRange($insert, $new)
  if ($sizeLine -ge 0) { $lines[$sizeLine] = "size=$size" } elseif ($new.Count -gt 0) { $lines.Insert($insert + $new.Count, "size=$size") }
  Copy-Item -LiteralPath $Ini -Destination ($Ini + ".wjat-backup") -Force
  [IO.File]::WriteAllText($Ini, ([string]::Join($nl, $lines) + $nl), $enc)
  Say ("added: " + ($added -join ", "))
} catch { Say ("FAILED: " + $_) }
'''


def schedule_on_exit(tools: List[Tool], ini_path: str, work_dir: str, mo2_pid: Optional[int] = None) -> str:
    """Write the helper + entries and start it detached. Returns the log path."""
    os.makedirs(work_dir, exist_ok=True)
    script = os.path.join(work_dir, "add_executables.ps1")
    entries = os.path.join(work_dir, "pending_executables.json")
    log = os.path.join(work_dir, "add_executables.log")
    with open(script, "w", encoding="utf-8-sig") as h:
        h.write(PS_SCRIPT)
    payload = []
    for t in tools:
        lines = entry_lines(1, t)
        payload.append({
            "title": t.title,
            "lines": [re.sub(r"^\d+", "#", l) for l in lines],
            "values": {l.split("\\", 1)[1].split("=", 1)[0]: l.split("=", 1)[1] for l in lines},
        })
    with open(entries, "w", encoding="utf-8") as h:
        json.dump(payload, h, indent=1)
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-File", script, "-MoPid", str(mo2_pid or os.getpid()), "-Ini", ini_path,
            "-Entries", entries, "-Log", log]
    detached, new_group, breakaway = 0x00000008, 0x00000200, 0x01000000
    # Must outlive MO2: detach, and leave MO2's job object if it is in one.
    for flags in ((detached | new_group | breakaway), (detached | new_group)) if os.name == "nt" else (0,):
        try:
            subprocess.Popen(args, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("could not start PowerShell")
    return log
