# -*- coding: utf-8 -*-
"""mobase entry point."""

from __future__ import annotations

from typing import List, Optional

import mobase
from PyQt6.QtGui import QIcon

from .common import PLUGIN_DISPLAY, PLUGIN_NAME, PLUGIN_VERSION


class NexusAvailabilityCheck(mobase.IPluginTool):
    def __init__(self) -> None:
        super().__init__()
        self._organizer: Optional[mobase.IOrganizer] = None
        self._parent_widget = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        return True

    # name() stays the same as v1 so existing plugin settings carry over.
    def name(self) -> str:
        return PLUGIN_NAME

    def author(self) -> str:
        return "Tvirus5087"

    def description(self) -> str:
        return ("Wabbajack author toolkit: checks Nexus/direct links are still downloadable "
                "(one API call per archive), matches downloads to installed mods and flags them "
                "installed, and runs a pre-compile preflight of compiler settings, Stock Game, "
                "Creation Club files and the published repository.")

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(*PLUGIN_VERSION, mobase.ReleaseType.BETA)

    def requirements(self) -> List[mobase.IPluginRequirement]:
        return []

    def settings(self) -> List[mobase.PluginSetting]:
        return [
            mobase.PluginSetting("max_concurrent_requests", "Nexus requests kept in flight at once.", 4),
            mobase.PluginSetting("request_timeout_seconds", "Give up on a single request after this many seconds.", 45),
            mobase.PluginSetting("cache_days", "Skip entries confirmed available within this many days (0 disables).", 7),
            mobase.PluginSetting("include_installed_mods", "Also check installed mods whose archive is gone.", True),
            mobase.PluginSetting("confirm_mod_pages", "Also request mod-page status (+1 Nexus call per mod).", False),
            mobase.PluginSetting("preflight_online", "Run the online part of the Wabbajack preflight.", True),
        ]

    def displayName(self) -> str:
        return PLUGIN_DISPLAY

    def tooltip(self) -> str:
        return "Nexus availability, download matching and Wabbajack preflight"

    def icon(self) -> QIcon:
        return QIcon()

    def setParentWidget(self, widget) -> None:
        self._parent_widget = widget

    def display(self) -> None:
        if self._organizer is None:
            return
        from .dialog import ToolkitDialog

        ToolkitDialog(self._organizer, self.name(), self._parent_widget).exec()
