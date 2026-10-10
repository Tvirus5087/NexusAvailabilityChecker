# -*- coding: utf-8 -*-
"""
Wabbajack Author Toolkit (formerly Nexus Availability Check) - MO2 2.5+ plugin.

Install: copy this whole folder to <MO2>/plugins/nexus_availability_check/
and delete the old single-file plugins/nexus_availability_check.py if present.

Modules:
  common.py     Qt-free helpers (INI editing, findings, image headers)
  model.py      Qt-free scanning + download <-> installed-mod matching
  preflight.py  Qt-free Wabbajack preflight checks
  workers.py    Qt network/download workers (Nexus bridge, HTTP)
  dialog.py     the UI
  plugin.py     mobase.IPluginTool entry point
"""


def createPlugin():
    from .plugin import NexusAvailabilityCheck

    return NexusAvailabilityCheck()
