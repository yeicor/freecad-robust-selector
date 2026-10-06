"""Real FreeCAD test suite: GUI Interactive Panel and Commands.

Executes within real FreeCAD GUI with Qt widgets and interactive commands.
"""
import unittest
import os
os.environ["FREECAD_NO_POPUP"] = "1"

try:
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtCore, QtWidgets
except ImportError:
    App = None
    Gui = None

from robust_selector import bind, create, selector
from fs_selector import Step


def setUpModule():
    if Gui is None:
        raise unittest.SkipTest("FreeCADGui not available in Python environment")


class TestRealFreeCADGui(unittest.TestCase):
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
        cls.doc = App.newDocument("TestRealGuiDoc")

    @classmethod
    def tearDownClass(cls):
        App.closeDocument(cls.doc.Name)

    def test_01_workbench_registered_and_active(self):
        """Verify RobustSelectorWorkbench is loaded and active in FreeCADGui."""
        self.assertEqual(Gui.activeWorkbench().name(), "RobustSelectorWorkbench")
        wbs = Gui.listWorkbenches()
        self.assertIn("RobustSelectorWorkbench", wbs)

    def test_02_panel_learn_and_route_planning(self):
        """Verify panel captures selection, auto-plans routes, and displays results."""
        from fs_gui import FeatureSelectorPanel
        panel = FeatureSelectorPanel.instance()
        panel.show_panel()
        self.assertTrue(Gui.Control.activeDialog())

        box = self.doc.addObject("Part::Box", "GuiBox")
        box.Length = 50.0
        box.Width = 40.0
        box.Height = 25.0
        self.doc.recompute()

        # Select top face (Face6) in FreeCAD 3D selection
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(box, "Face6")

        panel.learn()
        self.assertEqual(panel.source_obj.Name, "GuiBox")
        self.assertEqual(panel.kind, "Face")
        self.assertEqual(panel.captured_selection, ["Face6"])
        panel.autocomplete_btn.click()
        self._wait_for_planning(panel)
        self.assertTrue(len(panel.plans) > 0)
        self.assertIn("exact match", panel.current_result.text().lower())

    def test_03_panel_cadquery_expression_and_cursor_autocomplete(self):
        """Verify user can interact with CadQuery expression editor and cursor autocomplete."""
        from fs_gui import FeatureSelectorPanel
        panel = FeatureSelectorPanel.instance()
        panel.show_panel()

        # Direct expression editing
        panel.expr_edit.setPlainText(">Z")
        self.assertIn("Face", panel.current_result.text())

        # Test partial expression and cursor autocomplete
        panel.expr_edit.setPlainText("faces(\">Z\").edges(")
        cursor = panel.expr_edit.textCursor()
        cursor.setPosition(len("faces(\">Z\").edges("))
        panel.expr_edit.setTextCursor(cursor)
        panel.autocomplete_btn.click()
        self.assertTrue(len(panel.expr_edit.toPlainText()) > len("faces(\">Z\").edges("))

    def _test_04_panel_create_and_bind_workflow(self):
        """Verify the full Create + Bind workflow through the GUI panel."""
        from fs_gui import FeatureSelectorPanel
        panel = FeatureSelectorPanel.instance()

        box = self.doc.GuiBox
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(box, "Face6")
        panel.learn()

        # Add target sketch
        sketch = self.doc.addObject("Sketcher::SketchObject", "GuiSketch")
        sketch.MapMode = "FlatFace"
        self.doc.recompute()

        # Select target using use_selected_target
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(sketch)
        panel.use_selected_target()

        self.assertEqual(panel.target_combo.currentData(), "GuiSketch")
        self.assertIn("AttachmentSupport", panel.property_combo.currentText())

        # Click Create + Bind
        panel.bind_btn.click()

        # Verify selector object exists in document
        selectors = [o for o in self.doc.Objects if o.TypeId == "App::FeaturePython" and hasattr(o, "Query")]
        self.assertTrue(len(selectors) >= 1)
        selector_obj = selectors[-1]
        self.assertEqual(selector_obj.ResolutionStatus, "Resolved")
        self.assertIn("1 binding(s) up to date", selector_obj.BindingStatus)

        # Check sketch placement followed face Z=25
        self.doc.recompute()
        self.assertAlmostEqual(sketch.Placement.Base.z, 25.0, places=3)

        # Mutate box height to 45mm
        box.Height = 45.0
        self.doc.recompute()
        self.assertAlmostEqual(sketch.Placement.Base.z, 45.0, places=3)

    def _test_05_panel_unbind_workflow(self):
        """Verify unbinding via panel unbind button leaves geometry intact."""
        from fs_gui import FeatureSelectorPanel
        panel = FeatureSelectorPanel.instance()

        sketch = self.doc.GuiSketch
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(sketch)
        panel.use_selected_target()

        # Click Unbind target property
        panel.unbind_btn.click()
        self.assertIn("Removed 1 explicit binding", panel.status.text())

        # Recompute box: sketch should remain at current position without error
        self.doc.recompute()
        self.assertAlmostEqual(sketch.Placement.Base.z, 45.0, places=3)


    def test_07_taskview_double_click_uses_standard_edit_flow(self):
        """Double-clicking a selector must route through setEdit/unsetEdit."""
        from fs_gui import FeatureSelectorPanel
        from fs_selector import Selector
        from fs_document import create_selector_object
        panel = FeatureSelectorPanel.instance()
        gd = Gui.getDocument(self.doc.Name)
        self.assertIsNotNone(gd)

        box = self.doc.addObject("Part::Box", "TaskBox")
        box.Length, box.Width, box.Height = 20.0, 20.0, 10.0
        self.doc.recompute()
        sel = create_selector_object(
            self.doc, box, Selector("Face", (), expected_count=1),
            label="TaskSel", captured_selection=["Face6"],
        )
        self.doc.recompute()
        try:
            # Standard entry: edit tracks the object instead of a bare showDialog.
            self.assertTrue(gd.setEdit(sel, 0))
            self.assertTrue(bool(Gui.Control.activeDialog()))
            self.assertEqual(panel.selector_obj.Name, sel.Name)

            # Double-click reuses the same standard path (no exception, stays open).
            proxy = sel.ViewObject.Proxy
            self.assertTrue(proxy.doubleClicked(sel.ViewObject))
            self.assertTrue(bool(Gui.Control.activeDialog()))

            # Switching edit to another operation follows the standard switch.
            box2 = self.doc.addObject("Part::Box", "TaskBox2")
            self.doc.recompute()
            self.assertTrue(gd.setEdit(box2, 0))
            self.assertTrue(bool(Gui.Control.activeDialog()))
        finally:
            try:
                gd.resetEdit()
            except Exception:
                pass
            try:
                if Gui.Control.activeDialog():
                    Gui.Control.closeDialog()
            except Exception:
                pass

    def test_08_taskview_requests_close_before_replacing_native_task(self):
        """Opening our panel over a native task must ask first, never kill silently."""
        from fs_gui import FeatureSelectorPanel
        panel = FeatureSelectorPanel.instance()
        gd = Gui.getDocument(self.doc.Name)

        box = self.doc.getObject("TaskBox") or self.doc.addObject("Part::Box", "TaskBox")
        self.doc.recompute()
        try:
            # Occupy the task view with a native edit session.
            self.assertTrue(gd.setEdit(box, 0))
            self.assertTrue(bool(Gui.Control.activeDialog()))

            # Declining keeps the native task untouched.
            old_popup = os.environ.get("FREECAD_NO_POPUP", "")
            os.environ["FREECAD_NO_POPUP"] = "0"
            orig_question = QtWidgets.QMessageBox.question
            QtWidgets.QMessageBox.question = staticmethod(
                lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Cancel
            )
            try:
                self.assertFalse(panel.show_panel())
            finally:
                QtWidgets.QMessageBox.question = orig_question
                os.environ["FREECAD_NO_POPUP"] = old_popup
            self.assertTrue(bool(Gui.Control.activeDialog()))

            # Accepting (auto-accept under FREECAD_NO_POPUP) takes over cleanly.
            self.assertTrue(panel.show_panel())
            self.assertTrue(bool(Gui.Control.activeDialog()))
        finally:
            try:
                gd.resetEdit()
            except Exception:
                pass
            try:
                if Gui.Control.activeDialog():
                    Gui.Control.closeDialog()
            except Exception:
                pass


    def test_09_stale_background_results_are_dropped(self):
        """A superseded planning result must never overwrite newer state."""
        from fs_gui import FeatureSelectorPanel
        panel = FeatureSelectorPanel.instance()
        panel.show_panel()
        before = list(panel.plans)
        stale_gen = panel._plan_generation
        panel._next_plan_generation()
        applied = []
        panel._planning_succeeded(stale_gen, self.doc, applied.append, ["stale-plan"])
        self.assertEqual(applied, [])
        self.assertEqual(list(panel.plans), before)

    def test_10_streamlined_panel_ui_no_accordions(self):
        """Accordions are removed to simplify the UI; standard dialog buttons are available."""
        from fs_gui import FeatureSelectorPanel
        panel = FeatureSelectorPanel.instance()
        panel.show_panel()
        # Verify obsolete accordion sections are completely removed from panel
        self.assertFalse(hasattr(panel, "routes_section"))
        self.assertFalse(hasattr(panel, "candidates_section"))
        self.assertFalse(hasattr(panel, "advanced_section"))
        # Verify standard FreeCAD dialog buttons and window title are registered
        self.assertTrue(panel.getStandardButtons() > 0)
        self.assertEqual(panel.getTitle(), "Feature Selector")

    def test_11_learn_refuses_foreign_document_geometry(self):
        """Capturing geometry from another document is refused with guidance."""
        from fs_gui import FeatureSelectorPanel
        panel = FeatureSelectorPanel.instance()
        panel.show_panel()
        foreign = App.newDocument("ForeignGeometryDoc")
        try:
            box = foreign.addObject("Part::Box", "ForeignBox")
            box.Length, box.Width, box.Height = 10.0, 10.0, 10.0
            foreign.recompute()
            App.setActiveDocument(self.doc.Name)
            self.assertTrue(panel._refuse_foreign_source(box))
            self.assertIsNone(panel.source_obj)
            self.assertIn("active document", panel.status.text())
            local = self.doc.addObject("Part::Box", "LocalRefusalBox")
            self.doc.recompute()
            self.assertFalse(panel._refuse_foreign_source(local))
        finally:
            App.closeDocument(foreign.Name)

    def test_12_define_selector_first_and_use_as_target(self):
        """Verify defining a selector first and adopting its results as the target."""
        from fs_gui import FeatureSelectorPanel
        panel = FeatureSelectorPanel.instance()
        panel.show_panel()

        box = self.doc.addObject("Part::Box", "ReverseTargetBox")
        box.Length, box.Width, box.Height = 30.0, 30.0, 30.0
        self.doc.recompute()

        # User selects only the object (e.g. from tree view or 3D view), no subelements
        panel.source_obj = box
        panel.target_subnames = []
        panel.kind = None
        panel.expr_edit.setPlainText('faces(">Z").edges()')

        # Before setting as target: 4 edges evaluated, but no target set
        self.assertEqual(len(panel.target_subnames), 0)
        self.assertEqual(len(panel.last_resolved), 4)
        self.assertIn("Found 4 Edge(s)", panel.current_result.text())

        # Click Set as Target button
        panel.set_target_btn.click()

        # Target should now be adopted and be an exact match
        self.assertEqual(len(panel.target_subnames), 4)
        self.assertEqual(panel.kind, "Edge")
        self.assertIn("exact match", panel.current_result.text().lower())
        self.assertEqual(len(panel.plans), 1)

        # OK / accept saves the selector with the adopted targets
        selector = panel._exact_selector()
        self.assertIsNotNone(selector)
        self.assertEqual(selector.expected_count, 4)
        self.assertEqual(selector.expression, 'faces(">Z").edges()')


    def _test_06_streamlined_panel_ux(self):
        """Verify progressive disclosure, help guide, dynamic bind button, and no-target fallback."""
        from fs_gui import FeatureSelectorPanel
        panel = FeatureSelectorPanel.instance()

        # 1. Test collapsible sections toggle
        self.assertTrue(panel.routes_section.isCollapsed())
        panel.routes_section.expand()
        self.assertFalse(panel.routes_section.isCollapsed())
        panel.routes_section.collapse()
        self.assertTrue(panel.routes_section.isCollapsed())

        # 2. Test quick guide action without popup blocking
        panel.help_btn.click()

        # 3. Test capture on box face
        box = self.doc.GuiBox
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(box, "Face6")
        panel.learn()

        # Clear target selection so no target is picked
        panel.target_combo.setCurrentIndex(0)

        # Dynamic button should offer creation directly
        self.assertIn("Create Robust Selector", panel.bind_btn.text())

        # Clicking bind_btn when no target is picked falls back to saving
        panel.bind_btn.click()
        self.assertIsNotNone(panel.selector_obj)
        self.assertIn("Saved", panel.status.text())

        # Dynamic button now reflects saved state
        self.assertIn("Save Changes", panel.bind_btn.text())

        # 4. Test Python code export
        panel.copy_python_btn.click()
        self.assertIn("copied to clipboard", panel.status.text())


if __name__ == "__main__":
    import sys
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRealFreeCADGui)
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    os._exit(0 if res.wasSuccessful() else 1)
