# -*- coding: utf-8 -*-
"""Qt-free helpers shared by every part of the plugin.

Nothing in here imports mobase or PyQt, so it can be unit tested outside MO2.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

PLUGIN_NAME = "Nexus Availability Check"
PLUGIN_DISPLAY = "Wabbajack Author Toolkit"
PLUGIN_VERSION = (2, 5, 0)
DATA_SUBDIR = "nexus_availability_check"

# ---------------------------------------------------------------------------
# Findings (preflight / matching results)
# ---------------------------------------------------------------------------

SEV_ERROR = "ERROR"   # will break compilation, install, or gallery listing
SEV_WARN = "WARN"     # likely problem or moderator rejection reason
SEV_INFO = "INFO"     # worth knowing, nothing to fix necessarily
SEV_OK = "OK"         # check ran and passed

SEVERITY_ORDER = {SEV_ERROR: 0, SEV_WARN: 1, SEV_INFO: 2, SEV_OK: 3}


@dataclass
class Finding:
    severity: str
    area: str            # "Compiler settings", "Stock Game", "Repository", ...
    title: str
    detail: str = ""
    fix: str = ""
    path: str = ""       # local file/folder the finding is about, if any
    url: str = ""        # web page the finding is about, if any

    def as_dict(self) -> Dict[str, str]:
        return {
            "severity": self.severity,
            "area": self.area,
            "title": self.title,
            "detail": self.detail,
            "fix": self.fix,
            "path": self.path,
            "url": self.url,
        }


def sort_findings(findings: Iterable[Finding]) -> List[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.area))


def findings_markdown(findings: List[Finding], heading: str = "Wabbajack preflight") -> str:
    lines = [f"# {heading}", ""]
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITY_ORDER}
    lines.append(
        f"- Errors: **{counts[SEV_ERROR]}**  \n- Warnings: **{counts[SEV_WARN]}**  \n"
        f"- Info: **{counts[SEV_INFO]}**  \n- Passed: **{counts[SEV_OK]}**"
    )
    lines.append("")
    current = None
    for f in sort_findings(findings):
        if f.severity != current:
            current = f.severity
            lines += ["", f"## {current}", ""]
        lines.append(f"- **[{f.area}] {f.title}**")
        if f.detail:
            lines.append(f"  - {f.detail}")
        if f.fix:
            lines.append(f"  - Fix: {f.fix}")
        if f.path:
            lines.append(f"  - Path: `{f.path}`")
        if f.url:
            lines.append(f"  - Link: {f.url}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path))) if path else ""


def same_path(a: str, b: str) -> bool:
    return bool(a and b) and norm(a) == norm(b)


def is_inside(child: str, parent: str) -> bool:
    if not child or not parent:
        return False
    c, p = norm(child), norm(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives on Windows
        return False


def free_bytes(path: str) -> Optional[int]:
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except Exception:
        return None


def human_size(value: Optional[int]) -> str:
    if value is None or value < 0:
        return "?"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


# ---------------------------------------------------------------------------
# MO2 .meta / meta.ini (QSettings INI) reading and surgical editing
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return value


def read_ini(path: str) -> Dict[str, Dict[str, str]]:
    """Parse a QSettings-style INI into {section_lower: {key_lower: value}}.

    Tolerates MO2's quoted values and @Variant blobs (kept as raw text), which
    configparser chokes on. Keys before any section header land in 'general'.
    """
    data: Dict[str, Dict[str, str]] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return data
    section = "general"
    for line in lines:
        if not line.strip() or line.lstrip().startswith((";", "#")):
            continue
        match = _SECTION_RE.match(line)
        if match:
            section = match.group("name").strip().lower()
            data.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data.setdefault(section, {})[key.strip().lower()] = _unquote(value)
    return data


def meta_get(meta: Dict[str, Dict[str, str]], *keys: str) -> str:
    general = meta.get("general", {})
    for key in keys:
        value = general.get(key.lower())
        if value is not None and value.strip() != "":
            return value.strip()
    return ""


def to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip().strip('"'))
    except (TypeError, ValueError):
        return None


def to_bool(value: Optional[str]) -> bool:
    return str(value or "").strip().strip('"').lower() in ("true", "1", "yes")


def set_ini_values(path: str, values: Dict[str, str], section: str = "General") -> bool:
    """Set keys in one section of an INI without touching any other line.

    QSettings rewrites the whole file (and reorders/re-escapes it); this keeps
    @Variant blobs, comments and line endings byte-for-byte. Returns True when
    the file changed. Writes atomically via a temp file.
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    text = raw.decode("utf-8", errors="surrogateescape")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if not match:
            continue
        if start is None and match.group("name").strip().lower() == section.lower():
            start = i
        elif start is not None:
            end = i
            break

    if start is None:
        lines = [f"[{section}]"] + lines
        start, end = 0, 1
        # every original line moved down one
        for i in range(1, len(lines)):
            if _SECTION_RE.match(lines[i]):
                end = i
                break
        else:
            end = len(lines)

    remaining = {k.lower(): (k, v) for k, v in values.items()}
    changed = False
    for i in range(start + 1, end):
        if "=" not in lines[i]:
            continue
        key = lines[i].split("=", 1)[0].strip()
        if key.lower() in remaining:
            orig_key, value = remaining.pop(key.lower())
            new_line = f"{key}={value}"
            if lines[i] != new_line:
                lines[i] = new_line
                changed = True

    if remaining:
        insert_at = end
        # keep the new keys above trailing blank lines of the section
        while insert_at - 1 > start and not lines[insert_at - 1].strip():
            insert_at -= 1
        new_lines = [f"{k}={v}" for (k, v) in remaining.values()]
        lines[insert_at:insert_at] = new_lines
        changed = True

    if not changed:
        return False

    out = newline.join(lines)
    if raw.endswith((b"\n", b"\r\n")):
        out += newline
    tmp = path + ".wjat.tmp"
    with open(tmp, "wb") as handle:
        handle.write(out.encode("utf-8", errors="surrogateescape"))
    os.replace(tmp, path)
    return True


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def parse_version(text: str) -> Optional[Tuple[int, int, int, int]]:
    if not text:
        return None
    match = re.fullmatch(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?\s*", str(text))
    if not match:
        return None
    parts = [int(p) if p is not None else 0 for p in match.groups()]
    return (parts[0], parts[1], parts[2], parts[3])


def version_str(v: Optional[Tuple[int, ...]]) -> str:
    return ".".join(str(p) for p in v) if v else "?"


# ---------------------------------------------------------------------------
# Image headers (no Qt / PIL needed)
# ---------------------------------------------------------------------------


def image_size(path: str) -> Optional[Tuple[int, int, str]]:
    """Return (width, height, format) for PNG / JPEG / WEBP, or None."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
                return w, h, "PNG"
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                chunk = head[12:16]
                if chunk == b"VP8X":
                    w = int.from_bytes(head[24:27], "little") + 1
                    h = int.from_bytes(head[27:30], "little") + 1
                    return w, h, "WEBP"
                if chunk == b"VP8 ":
                    w, h = struct.unpack("<HH", head[26:30])
                    return w & 0x3FFF, h & 0x3FFF, "WEBP"
                if chunk == b"VP8L":
                    b = head[21:25]
                    w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                    h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                    return w, h, "WEBP"
                return None
            if head[:2] == b"\xff\xd8":
                handle.seek(2)
                while True:
                    marker = handle.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    code = marker[1]
                    if code in (0xD8, 0x01) or 0xD0 <= code <= 0xD7:
                        continue
                    length = struct.unpack(">H", handle.read(2))[0]
                    if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
                        handle.read(1)
                        h, w = struct.unpack(">HH", handle.read(4))
                        return w, h, "JPEG"
                    handle.seek(length - 2, 1)
    except (OSError, struct.error):
        return None
    return None


# ---------------------------------------------------------------------------
# MO2 profile files
# ---------------------------------------------------------------------------


@dataclass
class ProfileMod:
    name: str
    enabled: bool
    separator: bool = False
    foreign: bool = False   # '*' lines: DLC / unmanaged


def read_modlist(profile_dir: str) -> List[ProfileMod]:
    path = os.path.join(profile_dir, "modlist.txt")
    out: List[ProfileMod] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\r\n")
                if not line or line.startswith("#"):
                    continue
                flag, name = line[0], line[1:]
                out.append(
                    ProfileMod(
                        name=name,
                        enabled=flag == "+",
                        separator=name.endswith("_separator"),
                        foreign=flag == "*",
                    )
                )
    except OSError:
        pass
    return out


def read_plugins(profile_dir: str) -> List[Tuple[str, bool]]:
    path = os.path.join(profile_dir, "plugins.txt")
    out: List[Tuple[str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                enabled = line.startswith("*")
                out.append((line.lstrip("*"), enabled))
    except OSError:
        pass
    return out
