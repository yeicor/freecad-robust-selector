"""FreeCAD Robust Selector workbench GUI initialization.

Architectural Scope Boundary (MIN-4):
FreeCAD FeatureSelector is strictly focused on robust semantic geometric selection
and transparent property binding.
Strict boundaries: Strictly forbids mesh generation, direct modeling/booleans,
or non-native dependencies outside FreeCAD / PySide / OCC.
"""
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

if hasattr(Gui, "addIconPath"):
    Gui.addIconPath(os.path.join(_ROOT, "icons"))

from fs_commands import register_commands


class RobustSelectorWorkbench(Gui.Workbench):
    """Workbench hosting the explicit semantic-selector workflow."""

    MenuText = "Robust Selector"
    ToolTip = "Explicit, persistent robust semantic selectors for FreeCAD"
    Icon = "FeatureSelector.svg"

    def Initialize(self):
        # NOTE: FreeCAD executes InitGui.py in the shared __main__ namespace,
        # whose globals may be cleared or reused by the time the user first
        # activates the workbench. That used to break activation with
        # "NameError: name 'register_commands' is not defined". Import locally
        # so activation never depends on InitGui module globals surviving.
        # The instance itself is kept alive by FreeCAD, so an instance-level
        # guard is safe against double initialization.
        if getattr(self, "_initialized", False):
            return
        from fs_commands import (
            CONTEXT_COMMANDS,
            MENU_COMMANDS,
            TOOLBAR_COMMANDS,
            register_commands,
        )

        self._commands = register_commands()
        self.appendToolbar("Robust Selection", TOOLBAR_COMMANDS)
        self.appendMenu(["Robust Selection"], MENU_COMMANDS)
        self.appendContextMenu("Robust Selection", CONTEXT_COMMANDS)
        self._initialized = True

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
