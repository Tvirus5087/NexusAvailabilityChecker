# -*- coding: utf-8 -*-
"""Collection -> MO2 build pipeline. Qt-free so it can be tested headless.

plan_build()  decides, per selected mod, whether it is already installed,
              installable from an archive, bundled, or must be skipped.
run_build()   installs (FOMOD options are read and applied directly, never
              by launching an installer), then writes separators, a new
              profile and a report.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import collection as C
from . import installer as I
from . import profile as P


@dataclass
class BuildPlan:
    coll: C.Collection
    selected: Set[str]
    order: List[str]
    jobs: Dict[str, I.InstallJob]
    folder_of: Dict[str, str]
    skipped: Dict[str, str]
    mods_path: str
    profiles_dir: str
    profile_name: str
    copy_settings_from: str = ""
    use_separators: bool = True
    game_data_dir: str = ""
    work_dir: str = ""
    verify_md5: bool = False
    cycles: List[str] = field(default_factory=list)


def _bundle_source(bundle_root: str, mod: C.CollectionMod) -> str:
    if not bundle_root:
        return ""
    base = I.resolve_ci(bundle_root, "bundled") or bundle_root
    for candidate in (mod.logical_filename, mod.file_expression, mod.name):
        if candidate:
            hit = I.resolve_ci(base, candidate)
            if hit:
                return hit
    return ""


def unique_profile_name(profiles_dir: str, wanted: str) -> str:
    name = I.safe_folder_name(wanted)
    n = 2
    candidate = name
    while os.path.exists(os.path.join(profiles_dir, candidate)):
        candidate = f"{name} ({n})"
        n += 1
    return candidate


def plan_build(coll: C.Collection, selected: Set[str], *, downloads_path: str, mods_path: str,
               profiles_dir: str, profile_name: str, game: str, overwrite: bool = False,
               bundle_root: str = "", root_override: Optional[Set[str]] = None, **kwargs) -> BuildPlan:
    mods = coll.by_key()
    order_res = P.build_order(coll, selected)
    jobs: Dict[str, I.InstallJob] = {}
    folder_of: Dict[str, str] = {}
    skipped: Dict[str, str] = {}
    taken: Set[str] = set()
    for key in order_res.keys:
        mod = mods[key]
        if mod.local == C.LOCAL_INSTALLED and mod.local_detail and not overwrite:
            folder_of[key] = mod.local_detail
            taken.add(mod.local_detail.lower())
            continue
        folder = I.safe_folder_name(mod.name)
        if folder.lower() in taken or (not overwrite and os.path.isdir(os.path.join(mods_path, folder))):
            folder = I.safe_folder_name(mod.name, f" [{mod.file_id or key}]")
        taken.add(folder.lower())
        job = I.InstallJob(
            key=key, mod_name=folder, archive="", game=game, mod_id=mod.mod_id, file_id=mod.file_id,
            version=mod.version, category=mod.category, choices=(mod.raw or {}).get("choices"),
            md5=mod.md5, hashes=(mod.raw or {}).get("hashes") or None, has_patches=mod.has_patches,
            mod_type="root" if key in (root_override or set()) else mod.mod_type,
            nexus_url=(f"https://www.nexusmods.com/{mod.domain or coll.domain}/mods/{mod.mod_id}"
                       if mod.mod_id else ""),
        )
        if mod.source_type == C.SRC_BUNDLE:
            src = _bundle_source(bundle_root, mod)
            if not src:
                skipped[key] = "bundled file not found - use 'Get collection.json' so the archive is available"
                continue
            if os.path.isdir(src):
                job.bundled_dir = src
            else:
                job.archive = src
        elif mod.local == C.LOCAL_ARCHIVE and mod.local_detail:
            job.archive = os.path.join(downloads_path, mod.local_detail)
        elif mod.local == C.LOCAL_INSTALLED:
            # overwrite requested but we only know the installed folder, not the archive
            folder_of[key] = mod.local_detail
            continue
        else:
            skipped[key] = ("archive not downloaded" if mod.local == C.LOCAL_MISSING
                            else "manual download - install it yourself")
            continue
        jobs[key] = job
        folder_of[key] = folder
    return BuildPlan(coll=coll, selected=set(selected), order=order_res.keys, jobs=jobs,
                     folder_of=folder_of, skipped=skipped, mods_path=mods_path,
                     profiles_dir=profiles_dir, profile_name=profile_name,
                     cycles=order_res.cycles, **kwargs)


@dataclass
class BuildReport:
    results: List[I.InstallResult] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)
    profile_path: str = ""
    separators_made: int = 0
    plugins: Optional[P.PluginPlan] = None
    conflicts_total: int = 0
    conflicts: List[Tuple[str, str, int, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    stopped: bool = False

    def markdown(self, plan: BuildPlan) -> str:
        mods = plan.coll.by_key()
        ok = [r for r in self.results if r.ok]
        bad = [r for r in self.results if not r.ok]
        review = [r for r in ok if r.notes]
        lines = [f"# Collection build: {plan.coll.name}", "",
                 f"Generated {datetime.now().isoformat(timespec='seconds')}", "",
                 f"- Installed: **{len(ok)}**, failed: **{len(bad)}**, skipped: **{len(self.skipped)}**, "
                 f"already installed: **{len(plan.folder_of) - len(plan.jobs)}**",
                 f"- Profile: `{self.profile_path or 'not written'}`"]
        if self.plugins:
            lines.append(f"- Plugins: {len(self.plugins.order)} written; "
                         f"{len(self.plugins.missing)} in the curator's list but not provided; "
                         f"{len(self.plugins.extra)} extra appended")
        lines += [f"- Note: {n}" for n in self.notes]
        if bad:
            lines += ["", "## Failed", ""] + [f"- **{r.mod_name}**: {'; '.join(r.notes)}" for r in bad]
        if self.skipped:
            lines += ["", "## Skipped", ""] + [f"- {mods[k].name}: {why}" for k, why in self.skipped.items()]
        if review:
            lines += ["", "## Review", ""] + [f"- **{r.mod_name}** ({r.mode}): {'; '.join(r.notes)}" for r in review]
        if self.plugins and self.plugins.missing:
            lines += ["", "## Plugins in the curator's list that no mod provided", "",
                      "Usually one of: a mod above failed or was skipped; a FOMOD patch the curator's "
                      "installer only adds when its master is present (e.g. Creation Club content you "
                      "do not own - common for `*_CC_*` patches); or an optional mod you did not select.", ""]
            lines += [f"- {p}" for p in self.plugins.missing]
        if self.plugins and self.plugins.extra:
            lines += ["", "## Plugins not in the curator's list (appended, enabled)", ""] + [f"- {p}" for p in self.plugins.extra]
        if self.conflicts:
            lines += ["", f"## Overwrites no curator rule decides ({self.conflicts_total} files overwritten in total)", "",
                      "Order chosen by category; confirm the winner is right.", ""]
            lines += [f"- {w} overwrites {l}: {n} file(s), e.g. `{ex}`" for w, l, n, ex in self.conflicts]
        if plan.cycles:
            lines += ["", "## Rule cycle", "", "These mods have contradicting before/after rules:", ""]
            lines += [f"- {mods[k].name}" for k in plan.cycles]
        if plan.coll.tools:
            lines += ["", "## Curator tools (add as MO2 executables if you need them)", ""]
            lines += [f"- {t.get('name')}: `{t.get('exe')}`" for t in plan.coll.tools]
        return "\n".join(lines) + "\n"


def run_build(plan: BuildPlan, extractor, progress: Callable[[int, int, str], None],
              result_cb: Callable[[I.InstallResult], None] = lambda r: None,
              should_stop: Callable[[], bool] = lambda: False) -> BuildReport:
    report = BuildReport(skipped=dict(plan.skipped))
    leftovers = I.cleanup_staging(plan.mods_path)
    if leftovers:
        report.notes.append(f"removed {len(leftovers)} half-installed folder(s) from an earlier build")
    work = plan.work_dir or os.path.join(plan.mods_path, ".wjat-work")
    os.makedirs(work, exist_ok=True)

    # FOMOD fileDependency checks see the game Data plus every mod installed so far.
    state = I.plugin_state_from([plan.game_data_dir] + [
        os.path.join(plan.mods_path, f) for k, f in plan.folder_of.items() if k not in plan.jobs])
    ordered_jobs = [plan.jobs[k] for k in plan.order if k in plan.jobs]
    total = len(ordered_jobs)
    for i, job in enumerate(ordered_jobs, 1):
        if should_stop():
            report.stopped = True
            report.notes.append(f"stopped after {i - 1} of {total} installs")
            break
        progress(i, total, job.mod_name)
        res = I.install_job(job, plan.mods_path, work, extractor, state, plan.verify_md5)
        report.results.append(res)
        result_cb(res)
        if res.ok:
            for name in os.listdir(os.path.join(plan.mods_path, job.mod_name)):
                if name.lower().endswith((".esp", ".esm", ".esl")):
                    state.present.add(name.lower())  # type: ignore[attr-defined]
    try:
        os.rmdir(work)
    except OSError:
        pass

    failed = {r.key for r in report.results if not r.ok}
    installed_keys = [k for k in plan.order if k in plan.folder_of and k not in failed
                      and os.path.isdir(os.path.join(plan.mods_path, plan.folder_of[k]))]
    if report.stopped:
        report.notes.append("profile not written because the build was stopped")
        return report

    progress(total, total, "writing profile")
    entries = P.with_separators(installed_keys, plan.coll, plan.folder_of, plan.use_separators)
    report.separators_made = P.ensure_separator_folders(plan.mods_path, entries)
    mod_dirs = [os.path.join(plan.mods_path, plan.folder_of[k]) for k in installed_keys]
    game_plugins: Dict[str, str] = {}
    try:
        for name in os.listdir(plan.game_data_dir):
            if name.lower().endswith((".esp", ".esm", ".esl")):
                game_plugins[name.lower()] = name
    except OSError:
        pass
    report.plugins = P.plan_plugins(plan.coll, P.plugins_in(mod_dirs), game_plugins)
    if plan.coll.exclude_plugin_rules:
        report.notes.append("the curator exported no plugin rules - sort the load order with LOOT")
    if not plan.coll.plugins:
        report.notes.append("no plugin list in collection.json - plugins enabled alphabetically; run LOOT")
    report.profile_path = P.write_profile(plan.profiles_dir, plan.profile_name, entries,
                                          report.plugins, plan.copy_settings_from)
    progress(total, total, "checking file conflicts")
    report.conflicts_total, report.conflicts = P.unruled_conflicts(
        plan.coll, installed_keys, plan.folder_of, plan.mods_path)
    pruned = P.prune_separators(plan.mods_path, plan.profiles_dir)
    if pruned:
        report.notes.append(f"removed {len(pruned)} empty separator folder(s) no profile uses")
    return report
