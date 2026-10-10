# -*- coding: utf-8 -*-
"""Nexus collection model. Qt-free.

Original implementation. It reads two documented inputs:
  * the Nexus v2 GraphQL `collectionRevision` object (mod list, optional
    flags, files, categories), and
  * the curator's `collection.json` from the collection archive (rules,
    FOMOD choices, bundled/patch markers, per-mod instructions).
Only field names are taken from those formats; no Vortex code is used.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .model import KIND_DIRECT, KIND_MANUAL, KIND_NEXUS, Entry, MatchReport

GRAPHQL_URL = "https://api.nexusmods.com/v2/graphql"
API_BASE = "https://api.nexusmods.com"

SRC_NEXUS = "nexus"
SRC_BUNDLE = "bundle"
SRC_DIRECT = "direct"
SRC_BROWSE = "browse"
SRC_MANUAL = "manual"

LOCAL_INSTALLED = "installed"
LOCAL_ARCHIVE = "archive in downloads"
LOCAL_MISSING = "missing"
LOCAL_BUNDLED = "bundled (in collection archive)"
LOCAL_MANUAL = "manual download"


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"nexusmods\.com/(?:games/)?(?P<domain>[\w-]+)/collections/(?P<slug>[\w]+)"
    r"(?:/revisions/(?P<rev>\d+))?",
    re.I,
)


def parse_collection_ref(text: str) -> Tuple[str, str, Optional[int]]:
    """(game domain, slug, revision or None) from a URL or a bare slug."""
    text = (text or "").strip()
    match = _URL_RE.search(text)
    if match:
        rev = match.group("rev")
        return match.group("domain").lower(), match.group("slug"), int(rev) if rev else None
    if re.fullmatch(r"[A-Za-z0-9]{4,12}", text):
        return "", text, None
    return "", "", None


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

# Field tree for the revision query. Names come from Nexus's public GraphQL
# types; anything this server build does not know is stripped and retried
# (see strip_failing_fields), so an unknown field never breaks the import.
Field = Any  # str | (str, [Field])

REVISION_FIELDS: List[Field] = [
    "revisionNumber", "downloadLink", "totalSize", "modCount", "installationInfo", "adultContent",
    ("gameVersions", ["reference"]),
    ("collection", ["name", "slug", ("user", ["name"]), ("game", ["domainName", "name"])]),
    ("externalResources", ["name", "resourceType", "resourceUrl", "optional"]),
    ("modFiles", [
        "optional", "fileId", "updatePolicy",
        ("file", [
            "fileId", "modId", "name", "version", "uri", "sizeInBytes", "size", "categoryId",
            ("mod", ["modId", "name", "author", "category", ("modCategory", ["name"]),
                     ("game", ["domainName"])]),
        ]),
    ]),
]
# Without these the import is meaningless, so they are never stripped.
PROTECTED_FIELDS = {"modFiles", "file", "fileId", "modId", "optional", "collection", "name"}


def render_fields(fields: List[Field], indent: int = 4) -> str:
    pad = " " * indent
    lines = []
    for f in fields:
        if isinstance(f, tuple):
            name, sub = f
            if not sub:
                continue
            lines.append(f"{pad}{name} {{\n{render_fields(sub, indent + 2)}\n{pad}}}")
        else:
            lines.append(f"{pad}{f}")
    return "\n".join(lines)


def _strip(fields: List[Field], name: str) -> Tuple[List[Field], bool]:
    out: List[Field] = []
    removed = False
    for f in fields:
        fname = f[0] if isinstance(f, tuple) else f
        if fname == name:
            removed = True
            continue
        if isinstance(f, tuple):
            sub, hit = _strip(f[1], name)
            removed = removed or hit
            if sub:
                out.append((f[0], sub))
            else:
                removed = True
        else:
            out.append(f)
    return out, removed


_BAD_FIELD_RE = re.compile(r"""field\s+["'](\w+)["']""", re.I)


def strip_failing_fields(fields: List[Field], errors: List[str]) -> Tuple[List[Field], List[str]]:
    """Drop fields the server rejected. Returns (new tree, removed names);
    an empty removed list means the errors were not about unknown fields."""
    removed: List[str] = []
    for message in errors:
        for name in _BAD_FIELD_RE.findall(message):
            if name in PROTECTED_FIELDS or name in removed:
                continue
            fields, hit = _strip(fields, name)
            if hit:
                removed.append(name)
    return fields, removed


def graphql_request(slug: str, revision: Optional[int],
                    fields: Optional[List[Field]] = None) -> Dict[str, Any]:
    """Body for POST to GRAPHQL_URL."""
    var_defs = "$slug: String!, $viewAdultContent: Boolean"
    args = "slug: $slug, viewAdultContent: $viewAdultContent"
    variables: Dict[str, Any] = {"slug": slug, "viewAdultContent": True}
    if revision:
        var_defs += ", $revision: Int!"
        args += ", revision: $revision"
        variables["revision"] = int(revision)
    body = render_fields(fields if fields is not None else REVISION_FIELDS)
    query = f"query CollectionImport({var_defs}) {{\n  collectionRevision({args}) {{\n{body}\n  }}\n}}"
    return {"query": query, "variables": variables, "operationName": "CollectionImport"}


def graphql_errors(payload: Dict[str, Any]) -> List[str]:
    return [str(e.get("message", e)) for e in (payload.get("errors") or [])]


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class CollectionMod:
    key: str                         # stable id within the collection
    name: str
    optional: bool = False
    source_type: str = SRC_NEXUS
    domain: str = ""
    mod_id: int = 0
    file_id: int = 0
    file_name: str = ""              # Nexus file display name
    logical_filename: str = ""
    file_expression: str = ""
    md5: str = ""
    size: Optional[int] = None
    version: str = ""
    author: str = ""
    category: str = ""
    update_policy: str = ""
    url: str = ""
    instructions: str = ""
    phase: int = 0
    has_choices: bool = False
    has_patches: bool = False
    has_hashes: bool = False
    mod_type: str = ""               # Vortex mod type (details.type), e.g. "dinput" = game root
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    # filled in by local_status / closure
    local: str = ""
    local_detail: str = ""
    required_by: List[str] = field(default_factory=list)

    @property
    def notes(self) -> str:
        bits = []
        if self.has_choices:
            bits.append("FOMOD choices")
        if self.has_patches:
            bits.append("patched")
        if self.has_hashes:
            bits.append("exact files")
        if self.phase:
            bits.append(f"phase {self.phase}")
        if self.instructions:
            bits.append("has instructions")
        return ", ".join(bits)


@dataclass
class CollectionRule:
    type: str
    source: str          # mod key
    reference: str       # mod key ("" when unresolved)
    raw_reference: Dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class Collection:
    name: str = ""
    author: str = ""
    slug: str = ""
    revision: Optional[int] = None
    domain: str = ""
    install_info: str = ""
    game_versions: List[str] = field(default_factory=list)
    total_size: Optional[int] = None
    download_link: str = ""
    adult: bool = False
    mods: List[CollectionMod] = field(default_factory=list)
    rules: List[CollectionRule] = field(default_factory=list)
    unresolved_rules: int = 0
    external: List[Dict[str, Any]] = field(default_factory=list)
    plugins: List[Tuple[str, bool]] = field(default_factory=list)   # curator's plugin list, in order
    tools: List[Dict[str, Any]] = field(default_factory=list)
    exclude_plugin_rules: bool = False
    has_json: bool = False           # collection.json loaded (rules/choices known)
    has_api: bool = False            # GraphQL data loaded

    def by_key(self) -> Dict[str, CollectionMod]:
        return {m.key: m for m in self.mods}

    @property
    def mandatory(self) -> List[CollectionMod]:
        return [m for m in self.mods if not m.optional]

    @property
    def optional_mods(self) -> List[CollectionMod]:
        return [m for m in self.mods if m.optional]


def _key_for(mod_id: int, file_id: int, name: str, index: int) -> str:
    if file_id:
        return f"f{file_id}"
    if mod_id:
        return f"m{mod_id}-{index}"
    return f"n{index}:{name.lower()}"


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _category(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")


# -- GraphQL -> Collection ----------------------------------------------------


def parse_graphql(payload: Dict[str, Any], slug: str = "") -> Collection:
    rev = ((payload or {}).get("data") or {}).get("collectionRevision")
    if not rev:
        errors = graphql_errors(payload or {})
        raise ValueError("; ".join(errors) or "Nexus returned no collection revision")
    coll = rev.get("collection") or {}
    game = coll.get("game") or {}
    out = Collection(
        name=str(coll.get("name") or ""),
        author=str(((coll.get("user") or {}).get("name")) or ""),
        slug=str(coll.get("slug") or slug),
        revision=_int(rev.get("revisionNumber")) or None,
        domain=str(game.get("domainName") or "").lower(),
        install_info=str(rev.get("installationInfo") or ""),
        game_versions=[str(g.get("reference")) for g in (rev.get("gameVersions") or []) if g.get("reference")],
        total_size=_int(rev.get("totalSize")) or None,
        download_link=str(rev.get("downloadLink") or ""),
        adult=bool(rev.get("adultContent")),
        external=list(rev.get("externalResources") or []),
        has_api=True,
    )
    for index, item in enumerate(rev.get("modFiles") or []):
        f = item.get("file") or {}
        mod = f.get("mod") or {}
        file_id = _int(f.get("fileId") or item.get("fileId"))
        mod_id = _int(f.get("modId") or mod.get("modId"))
        size = _int(f.get("sizeInBytes")) or (_int(f.get("size")) * 1024 if f.get("size") else 0)
        name = str(mod.get("name") or f.get("name") or f"File {file_id}")
        out.mods.append(CollectionMod(
            key=_key_for(mod_id, file_id, name, index),
            name=name,
            optional=bool(item.get("optional")),
            source_type=SRC_NEXUS,
            domain=str((mod.get("game") or {}).get("domainName") or out.domain).lower(),
            mod_id=mod_id,
            file_id=file_id,
            file_name=str(f.get("name") or ""),
            logical_filename=str(f.get("name") or ""),
            size=size or None,
            version=str(f.get("version") or item.get("version") or ""),
            author=str(mod.get("author") or ""),
            category=_category(mod.get("modCategory")) or _category(mod.get("category")),
            update_policy=str(item.get("updatePolicy") or ""),
        ))
    for index, res in enumerate(out.external):
        name = str(res.get("name") or f"External resource {index + 1}")
        out.mods.append(CollectionMod(
            key=f"x{index}",
            name=name,
            optional=bool(res.get("optional")),
            source_type=SRC_BROWSE,
            url=str(res.get("resourceUrl") or ""),
            category="External",
            instructions=str(res.get("resourceType") or ""),
        ))
    return out


# -- collection.json -> Collection ---------------------------------------------


def parse_collection_json(text: str) -> Collection:
    data = json.loads(text)
    if not isinstance(data, dict) or "mods" not in data:
        raise ValueError("not a collection.json (no 'mods' list)")
    info = data.get("info") or {}
    out = Collection(
        name=str(info.get("name") or ""),
        author=str(info.get("author") or ""),
        domain=str(info.get("domainName") or "").lower(),
        install_info=str(info.get("installInstructions") or ""),
        game_versions=[str(v) for v in (info.get("gameVersions") or [])],
        plugins=[(str(p.get("name") or ""), bool(p.get("enabled", True)))
                 for p in (data.get("plugins") or []) if isinstance(p, dict) and p.get("name")],
        tools=[t for t in (data.get("tools") or []) if isinstance(t, dict)],
        exclude_plugin_rules=bool((data.get("collectionConfig") or {}).get("excludePluginRules")),
        has_json=True,
    )
    for index, item in enumerate(data.get("mods") or []):
        src = item.get("source") or {}
        stype = str(src.get("type") or SRC_NEXUS).lower()
        mod_id, file_id = _int(src.get("modId")), _int(src.get("fileId"))
        name = str(item.get("name") or src.get("logicalFilename") or f"Mod {index + 1}")
        details = item.get("details") or {}
        out.mods.append(CollectionMod(
            key=_key_for(mod_id, file_id, name, index) if stype == SRC_NEXUS else f"j{index}",
            name=name,
            optional=bool(item.get("optional")),
            source_type=stype,
            domain=str(item.get("domainName") or out.domain).lower(),
            mod_id=mod_id,
            file_id=file_id,
            logical_filename=str(src.get("logicalFilename") or ""),
            file_expression=str(src.get("fileExpression") or ""),
            md5=str(src.get("md5") or "").lower(),
            size=_int(src.get("fileSize")) or None,
            version=str(item.get("version") or ""),
            author=str(item.get("author") or ""),
            category=_category(details.get("category")),
            update_policy=str(src.get("updatePolicy") or ""),
            url=str(src.get("url") or ""),
            instructions=str(item.get("instructions") or src.get("instructions") or ""),
            phase=_int(item.get("phase")),
            has_choices=bool(item.get("choices")),
            has_patches=bool(item.get("patches")),
            has_hashes=bool(item.get("hashes")),
            mod_type=str(details.get("type") or ""),
            raw=item,
        ))
    resolver = ReferenceResolver(out.mods)
    for rule in data.get("modRules") or []:
        src_key = resolver.resolve(rule.get("source") or {})
        ref_key = resolver.resolve(rule.get("reference") or {})
        if not src_key or not ref_key:
            out.unresolved_rules += 1
        out.rules.append(CollectionRule(str(rule.get("type") or ""), src_key, ref_key,
                                        rule.get("reference") or {}))
    return out


class ReferenceResolver:
    """Match a mod reference (md5 / repo ids / logical filename / file
    expression) to a mod in the collection."""

    def __init__(self, mods: List[CollectionMod]) -> None:
        self._by_file = {m.file_id: m.key for m in mods if m.file_id}
        self._by_md5 = {m.md5: m.key for m in mods if m.md5}
        self._by_logical = {m.logical_filename.lower(): m.key for m in mods if m.logical_filename}
        self._mods = mods

    def resolve(self, ref: Dict[str, Any]) -> str:
        repo = ref.get("repo") or {}
        file_id = _int(repo.get("fileId"))
        if file_id and file_id in self._by_file:
            return self._by_file[file_id]
        md5 = str(ref.get("fileMD5") or "").lower()
        if md5 and md5 in self._by_md5:
            return self._by_md5[md5]
        logical = str(ref.get("logicalFileName") or "").lower()
        if logical and logical in self._by_logical:
            return self._by_logical[logical]
        expression = str(ref.get("fileExpression") or "")
        if expression:
            pattern = expression.lower()
            for mod in self._mods:
                for candidate in (mod.file_expression, mod.logical_filename):
                    if candidate and (candidate.lower() == pattern or fnmatch.fnmatch(candidate.lower(), pattern)):
                        return mod.key
        return ""


def merge(api: Optional[Collection], local: Optional[Collection]) -> Collection:
    """collection.json is authoritative for rules and install details; the API
    fills in names, categories, sizes and revision metadata."""
    if api is None:
        return local or Collection()
    if local is None:
        return api
    by_file = {m.file_id: m for m in api.mods if m.file_id}
    for mod in local.mods:
        hit = by_file.get(mod.file_id) if mod.file_id else None
        if hit is None:
            continue
        mod.category = mod.category or hit.category
        mod.size = mod.size or hit.size
        mod.file_name = hit.file_name or mod.file_name
        mod.author = mod.author or hit.author
        if hit.name and (not mod.name or mod.name == mod.logical_filename):
            mod.name = hit.name
    local_files = {m.file_id for m in local.mods if m.file_id}
    extra = [m for m in api.mods if m.source_type == SRC_NEXUS and m.file_id not in local_files]
    local.mods += extra   # files the API lists that the json lacks (shouldn't happen, but keep them)
    local.slug, local.revision = api.slug, api.revision
    local.name = local.name or api.name
    local.author = local.author or api.author
    local.domain = local.domain or api.domain
    local.total_size, local.download_link, local.adult = api.total_size, api.download_link, api.adult
    local.game_versions = local.game_versions or api.game_versions
    local.external = api.external
    local.has_api = True
    return local


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def closure(coll: Collection, chosen_optional: Set[str]) -> Tuple[Set[str], Dict[str, List[str]]]:
    """Every mod to install: mandatory + chosen optional + anything those
    'require' (transitively). Returns (keys, {pulled-in key: [requested by]})."""
    mods = coll.by_key()
    selected = {m.key for m in coll.mods if not m.optional} | {k for k in chosen_optional if k in mods}
    requires: Dict[str, List[str]] = {}
    for rule in coll.rules:
        if rule.type == "requires" and rule.source and rule.reference:
            requires.setdefault(rule.source, []).append(rule.reference)
    pulled: Dict[str, List[str]] = {}
    stack = list(selected)
    while stack:
        key = stack.pop()
        for dep in requires.get(key, []):
            if dep not in selected:
                selected.add(dep)
                stack.append(dep)
            if mods.get(dep) and mods[dep].optional and dep not in chosen_optional:
                pulled.setdefault(dep, [])
                name = mods[key].name if key in mods else key
                if name not in pulled[dep]:
                    pulled[dep].append(name)
    return selected, pulled


def conflicts_in(coll: Collection, selected: Set[str]) -> List[Tuple[str, str]]:
    mods = coll.by_key()
    out = []
    for rule in coll.rules:
        if rule.type == "conflicts" and rule.source in selected and rule.reference in selected:
            out.append((mods[rule.source].name, mods[rule.reference].name))
    return out


# ---------------------------------------------------------------------------
# Local state
# ---------------------------------------------------------------------------


def annotate_local(coll: Collection, report: Optional[MatchReport]) -> None:
    """Mark each mod installed / archive present / missing, without Nexus."""
    installed_pairs: Dict[int, str] = {}
    installed_archives: Dict[str, str] = {}
    downloads_by_file: Dict[int, str] = {}
    downloads_by_name: Dict[str, str] = {}
    if report is not None:
        for mod in report.mods_by_name.values():
            for pair_mod, file_id in mod.file_pairs:
                installed_pairs.setdefault(file_id, mod.name)
            if mod.installation_file:
                installed_archives.setdefault(mod.installation_file.lower(), mod.name)
        for row in report.rows:
            dl = row.download
            if not dl.has_archive:
                continue
            if dl.file_id:
                downloads_by_file.setdefault(dl.file_id, dl.archive)
            downloads_by_name.setdefault(dl.archive.lower(), dl.archive)

    for mod in coll.mods:
        mod.local_detail = ""
        if mod.source_type == SRC_BUNDLE:
            mod.local = LOCAL_BUNDLED
            continue
        if mod.file_id and mod.file_id in installed_pairs:
            mod.local, mod.local_detail = LOCAL_INSTALLED, installed_pairs[mod.file_id]
            continue
        logical = mod.logical_filename.lower()
        if logical and logical in installed_archives:
            mod.local, mod.local_detail = LOCAL_INSTALLED, installed_archives[logical]
            continue
        if mod.file_id and mod.file_id in downloads_by_file:
            mod.local, mod.local_detail = LOCAL_ARCHIVE, downloads_by_file[mod.file_id]
            continue
        if logical and logical in downloads_by_name:
            mod.local, mod.local_detail = LOCAL_ARCHIVE, downloads_by_name[logical]
            continue
        mod.local = LOCAL_MISSING if mod.source_type in (SRC_NEXUS, SRC_DIRECT) else LOCAL_MANUAL


def to_entry(mod: CollectionMod, mo2_game: str) -> Optional[Entry]:
    """An availability/download Entry for a collection mod, or None."""
    if mod.source_type == SRC_NEXUS and mod.mod_id and mod.file_id:
        return Entry(source="collection", game=mo2_game or mod.domain, mod_id=mod.mod_id,
                     file_id=mod.file_id, kind=KIND_NEXUS, archive=mod.local_detail
                     if mod.local == LOCAL_ARCHIVE else "", mod_name=mod.name,
                     display_name=mod.file_name or mod.name, version=mod.version)
    if mod.source_type == SRC_DIRECT and mod.url:
        return Entry(source="collection", game=mo2_game, mod_id=0, file_id=None, kind=KIND_DIRECT,
                     url=mod.url, mod_name=mod.name, display_name=mod.name, version=mod.version)
    if mod.source_type in (SRC_BROWSE, SRC_MANUAL) and mod.url:
        return Entry(source="collection", game=mo2_game, mod_id=0, file_id=None, kind=KIND_MANUAL,
                     url=mod.url, prompt=mod.instructions, mod_name=mod.name, display_name=mod.name)
    return None


def nexus_file_page(mod: CollectionMod, domain: str) -> str:
    """Mod-manager download link: hands MO2 an nxm:// link without Premium."""
    base = f"https://www.nexusmods.com/{mod.domain or domain}/mods/{mod.mod_id}"
    return f"{base}?tab=files&file_id={mod.file_id}&nmm=1" if mod.file_id else f"{base}?tab=files"


# ---------------------------------------------------------------------------
# Persistence of the author's selection (step 2 builds the profile from it)
# ---------------------------------------------------------------------------


def selection_path(data_dir: str, coll: Collection) -> str:
    ident = coll.slug or re.sub(r"\W+", "_", coll.name or "collection")
    return os.path.join(data_dir, "collections", f"{ident}.selection.json")


def save_selection(data_dir: str, coll: Collection, chosen_optional: Set[str],
                   root_override: Optional[Set[str]] = None) -> str:
    path = selection_path(data_dir, coll)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    selected, pulled = closure(coll, chosen_optional)
    by_key = coll.by_key()
    payload = {
        "collection": {"name": coll.name, "slug": coll.slug, "revision": coll.revision,
                       "domain": coll.domain, "author": coll.author},
        "chosen_optional": sorted(chosen_optional),
        "root_override": sorted(root_override or []),
        "selected": [
            {"key": k, "name": by_key[k].name, "optional": by_key[k].optional,
             "source": by_key[k].source_type, "modId": by_key[k].mod_id,
             "fileId": by_key[k].file_id, "category": by_key[k].category,
             "required_by": pulled.get(k, [])}
            for k in sorted(selected, key=lambda k: by_key[k].name.lower())
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return path


def load_root_override(data_dir: str, coll: Collection) -> Set[str]:
    try:
        with open(selection_path(data_dir, coll), "r", encoding="utf-8") as handle:
            return set(json.load(handle).get("root_override") or [])
    except (OSError, ValueError):
        return set()


def load_selection(data_dir: str, coll: Collection) -> Optional[Set[str]]:
    path = selection_path(data_dir, coll)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return set(json.load(handle).get("chosen_optional") or [])
    except (OSError, ValueError):
        return None
