"""Real FreeCAD test suite: Advanced User Experience, 1-Click Robustify, Auditing, and Diagnostics.

Executes within real FreeCAD and FreeCADGui verifying:
- 1-click robustification of native features (Part::Fillet, PartDesign::Fillet)
- Document-wide semantic selector auditing and health diagnostics
- GUI candidate inspector table with geometric metrics
- Presets insertion and live 3D preview
- Smart learn() detecting consumer features
"""
import os
import unittest

os.environ["FREECAD_NO_POPUP"] = "1"

try:
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtCore, QtWidgets
except ImportError:
    App = None
    Gui = None

from fs_bindings import (
    audit_document_selectors,
    format_audit_report,
    inspect_feature_references,
    robustify_feature,
)
from fs_gui import FeatureSelectorPanel, PRESETS


def setUpModule():
    if Gui is None or App is None:
        raise unittest.SkipTest("FreeCADGui/App not available in environment")


class TestRealFreeCADAdvancedFeatures(unittest.TestCase):
    def _wait_for_planning(self, panel, timeout_ms=120000):
        task = panel._plan_task
        if task is not None:
            task.wait(timeout_ms)

    @classmethod
    def setUpClass(cls):
        os.environ["FREECAD_NO_POPUP"] = "1"
        QtWidgets.QMessageBox.information = staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Ok)
        QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Ok)
        QtWidgets.QMessageBox.critical = staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Ok)
        Gui.showMainWindow()
        Gui.activateWorkbench("RobustSelectorWorkbench")
        cls.doc = App.newDocument("TestAdvancedFeaturesDoc")

    @classmethod
    def tearDownClass(cls):
        App.closeDocument(cls.doc.Name)

    def test_01_inspect_feature_references(self):
        """Verify inspect_feature_references detects reference properties and subelements."""
        box = self.doc.addObject("Part::Box", "BoxInspect")
        box.Length = 30.0
        box.Width = 30.0
        box.Height = 30.0
        self.doc.recompute()

        fillet = self.doc.addObject("Part::Fillet", "FilletInspect")
        fillet.Base = box
        fillet.Edges = [(1, 2.0, 2.0), (2, 2.0, 2.0)]
        self.doc.recompute()

        refs_info = inspect_feature_references(fillet)
        self.assertTrue(len(refs_info) >= 1)
        info = next(r for r in refs_info if r["property"] == "Edges")
        self.assertEqual(info["property"], "Edges")
        self.assertEqual(info["mode"], "FilletEdges")
        self.assertEqual(info["source"].Name, "BoxInspect")
        self.assertEqual(info["subnames"], ["Edge1", "Edge2"])
        self.assertEqual(info["kind"], "Edge")

    def test_02_1click_robustify_native_part_fillet(self):
        """Verify 1-click robustify_feature converts a native Part::Fillet to a robust selector."""
        box = self.doc.addObject("Part::Box", "BoxFillet")
        box.Length = 40.0
        box.Width = 40.0
        box.Height = 20.0
        self.doc.recompute()

        # Fillet with Edge1 and Edge2
        fillet = self.doc.addObject("Part::Fillet", "FilletNative")
        fillet.Base = box
        fillet.Edges = [(1, 3.0, 3.0), (2, 3.0, 3.0)]
        self.doc.recompute()

        # Execute 1-click robustify
        selector_obj, plan = robustify_feature(fillet, self.doc, prop_name="Edges")
        self.assertIsNotNone(selector_obj)
        self.assertIsNotNone(plan)
        self.assertEqual(selector_obj.BaseObject.Name, "BoxFillet")
        self.assertEqual(selector_obj.FeatureKind, "Edge")
        self.assertEqual(selector_obj.ResolutionStatus, "Resolved")
        print("Hasattr Edges:", hasattr(fillet, "RobustSelector_Edges"))
        print("Hasattr Base:", hasattr(fillet, "RobustSelector_Base"))
        self.assertTrue(hasattr(fillet, "RobustSelector_Edges"))
        self.assertEqual(getattr(fillet, "RobustSelector_Edges"), selector_obj)

        pass

        # Recompute under box dimensions edit
        box.Length = 60.0
        box.Width = 50.0
        self.doc.recompute()
        self.assertIn("Up-to-date", fillet.State)
        self.assertEqual(selector_obj.HealthStatus, "OK")

    def test_03_smart_learn_on_consumer_feature(self):
        """Verify panel.learn() automatically detects a consumer feature and pre-populates target."""
        panel = FeatureSelectorPanel.instance()
        panel.show_panel()

        # Create a new non-robustified fillet
        box = self.doc.getObject("BoxFillet")
        fillet2 = self.doc.addObject("Part::Fillet", "FilletNative2")
        fillet2.Base = box
        fillet2.Edges = [(1, 2.0, 2.0), (2, 2.0, 2.0)]
        self.doc.recompute()

        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(fillet2)  # Whole consumer feature selected

        panel.learn()
        self._wait_for_planning(panel)
        self.assertEqual(panel.source_obj.Name, "BoxFillet")
        self.assertEqual(panel.kind, "Edge")
        self.assertTrue(len(panel.target_subnames) > 0)
        self.assertIn("EXACT MATCH", panel.current_result.text())

    def test_04_candidate_inspector_table_metrics(self):
        """Verify candidate table displays subelements, status, and geometric metrics."""
        panel = FeatureSelectorPanel.instance()
        # With BoxFillet and Edge selected from previous test:
        self.assertTrue(panel.candidate_table.rowCount() >= 2)

        # Check row 0 contents
        subname = panel.candidate_table.item(0, 0).text()
        status = panel.candidate_table.item(0, 1).text()
        metric = panel.candidate_table.item(0, 2).text()
        center = panel.candidate_table.item(0, 3).text()

        self.assertTrue(subname.startswith("Edge"))
        self.assertEqual(status, "✓ Exact")
        self.assertIn("mm", metric)  # Curve (XX mm)
        self.assertTrue(center.startswith("(") and center.endswith(")"))

        # Test selecting a row in candidate table (triggers 3D highlight)
        panel.candidate_table.selectRow(0)
        panel._candidate_row_selected()
        current_sel = Gui.Selection.getSelectionEx()
        self.assertTrue(len(current_sel) > 0)
        self.assertEqual(current_sel[0].SubElementNames, (subname,))

    def test_05_presets_combo_insertion(self):
        """Verify selecting a preset inserts that design intent step and replans."""
        panel = FeatureSelectorPanel.instance()
        initial_step_count = len(panel.steps)

        # Select a vertical edge preset: "All Vertical Edges (|| Z)"
        vert_idx = next(i for i, (label, _) in enumerate(PRESETS) if "Vertical" in label)
        panel.preset_combo.setCurrentIndex(vert_idx)
        self._wait_for_planning(panel)

        # Steps count should have updated
        self.assertTrue(len(panel.steps) >= initial_step_count)
        self.assertTrue(any(s.forced for s in panel.steps))

    def test_06_live_3d_preview_toggle(self):
        """Verify toggling live 3D preview updates FreeCAD selection highlights."""
        panel = FeatureSelectorPanel.instance()
        box_prev = self.doc.addObject("Part::Box", "BoxPreview")
        self.doc.recompute()

        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(box_prev, "Edge1")
        panel.learn()
        self._wait_for_planning(panel)

        self.assertTrue(panel.live_preview_cb.isChecked())

        # Toggle off
        panel.live_preview_cb.setChecked(False)
        self.assertEqual(len(Gui.Selection.getSelection()), 0)

        # Toggle on
        panel.live_preview_cb.setChecked(True)
        self.assertTrue(len(Gui.Selection.getSelection()) > 0)

    def test_07_audit_document_selectors(self):
        """Verify document audit accurately tracks health, renumbering, and resolution times."""
        audit = audit_document_selectors(self.doc)
        self.assertNotIn("error", audit)
        self.assertTrue(audit["total"] >= 1)
        print("Audit Selectors:", audit.get("selectors"))
        self.assertEqual(audit["errors"], 0)
        self.assertTrue(audit["healthy"] >= 1)

        report = format_audit_report(audit)
        self.assertIn("Robust Feature Selector Document Audit", report)
        self.assertIn("Total Selectors", report)
        self.assertIn("FilletNative", report)
        self.assertIn("Eval:", report)

    def test_08_gui_commands_registered(self):
        """Verify FeatureSelector_RobustifyFeature and FeatureSelector_Audit are available."""
        cmd_robustify = Gui.runCommand("FeatureSelector_RobustifyFeature")
        # Command should exist without throwing NameError
        all_cmds = Gui.listCommands()
        self.assertIn("FeatureSelector_RobustifyFeature", all_cmds)
        self.assertIn("FeatureSelector_Audit", all_cmds)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRealFreeCADAdvancedFeatures)
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    os._exit(0 if res.wasSuccessful() else 1)
