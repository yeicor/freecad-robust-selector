"""FreeCAD Feature Selector workbench GUI initialization."""
from __future__ import annotations

import os

import FreeCADGui as Gui

try:
    _ROOT = os.path.dirname(os.path.abspath(__file__))
except NameError:
    try:
        import fs_commands
        _ROOT = os.path.dirname(os.path.abspath(fs_commands.__file__))
    except (ImportError, AttributeError):
        import FreeCAD as App
        _ROOT = os.path.join(App.getUserAppDataDir(), "Mod", "FeatureSelector")

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
        # The workbench does not capture selection or show a panel implicitly.
        # The first, discoverable action is the explicit Robust Selection command.
        return

    def Deactivated(self):
        return

    def GetClassName(self):
        return "Gui::PythonWorkbench"


class FeatureSelectorWorkbench(RobustSelectorWorkbench):
    """Backwards-compatible alias for FeatureSelectorWorkbench."""
    pass


existing_wbs = Gui.listWorkbenches()
if "RobustSelectorWorkbench" not in existing_wbs:
    Gui.addWorkbench(RobustSelectorWorkbench())
if "FeatureSelectorWorkbench" not in existing_wbs:
    Gui.addWorkbench(FeatureSelectorWorkbench())

# Attempt to inject into PartDesign so the user doesn't have to switch workbenches
try:
    _commands = register_commands()
    pd = Gui.getWorkbench("PartDesignWorkbench")
    pd.appendToolbar("Robust Selection", TOOLBAR_COMMANDS)
except Exception:
    pass

