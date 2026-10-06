"""FreeCAD Robust Selector workbench GUI initialization."""
from __future__ import annotations

import inspect
import os
import sys

import FreeCADGui as Gui

try:
    _ROOT = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _ROOT = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

Gui.addIconPath(os.path.join(_ROOT, "icons"))

from fs_commands import CONTEXT_COMMANDS, MENU_COMMANDS, TOOLBAR_COMMANDS, register_commands


class RobustSelectorWorkbench(Gui.Workbench):
    """Workbench hosting the explicit semantic-selector workflow."""

    MenuText = "Robust Selector"
    ToolTip = "Explicit, persistent robust semantic selectors for FreeCAD"
    Icon = "FeatureSelector.svg"

    def Initialize(self):
        self._commands = register_commands()
        self.appendToolbar("Robust Selection", TOOLBAR_COMMANDS)
        self.appendMenu(["Robust Selection"], MENU_COMMANDS)
        self.appendContextMenu("Robust Selection", CONTEXT_COMMANDS)

    def Activated(self):
        pass

    def Deactivated(self):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


existing_wbs = Gui.listWorkbenches()
if "RobustSelectorWorkbench" not in existing_wbs:
    Gui.addWorkbench(RobustSelectorWorkbench())
register_commands()
