import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestExplicitArchitecture(unittest.TestCase):
    def test_init_has_no_implicit_runtime_setup(self):
        text = (ROOT / "Init.py").read_text()
        self.assertNotIn("addDocumentObserver", text)
        self.assertNotIn("QTimer", text)
        self.assertNotIn("addSelectionGate", text)

    def test_command_surface_is_explicit(self):
        text = (ROOT / "fs_commands.py").read_text()
        self.assertIn("FeatureSelector_Robustify", text)

        self.assertNotIn("DocumentObserver", text)

    def test_workbench_only_registers_normal_gui_hooks(self):
        text = (ROOT / "InitGui.py").read_text()
        self.assertIn("appendToolbar", text)
        self.assertIn("appendMenu", text)
        self.assertIn("appendContextMenu", text)
        self.assertIn('return "Gui::PythonWorkbench"', text)


if __name__ == "__main__":
    unittest.main()
