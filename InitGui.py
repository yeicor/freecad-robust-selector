"""FreeCAD Robust Selector workbench GUI initialization."""
from __future__ import annotations

import os

import FreeCADGui as Gui

_ROOT = os.path.dirname(os.path.abspath(__file__))
Gui.addIconPath(os.path.join(_ROOT, "icons"))

from fs_commands import CONTEXT_COMMANDS, MENU_COMMANDS, TOOLBAR_COMMANDS, register_commands


class RobustSelectorWorkbench(Gui.Workbench):
    """Workbench hosting the explicit semantic-selector workflow."""

    MenuText = "Robust Selector"
    ToolTip = "Explicit, persistent robust semantic selectors for FreeCAD"
    Icon = "FeatureSelector.svg"

    def __init__(self):
        self._commands = []
        self._initialized = False

    def Initialize(self):
        if self._initialized:
            return
        self._commands = register_commands()
        self.appendToolbar("Robust Selection", TOOLBAR_COMMANDS)
        self.appendMenu(["Robust Selection"], MENU_COMMANDS)
        self.appendContextMenu("Robust Selection", CONTEXT_COMMANDS)
        self._initialized = True

    def Activated(self):
        return

    def Deactivated(self):
        return

    def GetClassName(self):
        return "Gui::PythonWorkbench"


existing_wbs = Gui.listWorkbenches()
if "RobustSelectorWorkbench" not in existing_wbs:
    Gui.addWorkbench(RobustSelectorWorkbench())

pd = Gui.getWorkbench("PartDesignWorkbench")
if pd is not None and hasattr(pd, "appendToolbar"):
    register_commands()
    pd.appendToolbar("Robust Selection", TOOLBAR_COMMANDS)
