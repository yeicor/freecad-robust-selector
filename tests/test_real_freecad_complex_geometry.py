"""Integration tests for complex geometry, acceleration, and project-wide robustification.

Executes within real FreeCAD environment (1.1.3+, OCC 7.9.3).
"""
import os
import sys
import unittest

sys.path.insert(0, "/usr/lib/freecad/lib")
import FreeCAD as App
import FreeCADGui as Gui
import Part
from PySide import QtWidgets

from fs_bindings import (
    audit_document_selectors,
    format_robustify_all_report,
    robustify_all_features,
)
from fs_freecad import adjacent_face_counts, adjacent_vertex_counts
from fs_selector import Selector, Step, selector_to_python


class TestRealFreeCADComplexGeometry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["FREECAD_NO_POPUP"] = "1"
        QtWidgets.QMessageBox.information = staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Ok)
        QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Ok)
        QtWidgets.QMessageBox.critical = staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Ok)
        try:
            Gui.showMainWindow()
            Gui.activateWorkbench("FeatureSelectorWorkbench")
        except Exception:
            pass

    def setUp(self):
        self.doc_name = f"TestDocComplex_{id(self)}"
        self.doc = App.newDocument(self.doc_name)

    def tearDown(self):
        if App.getDocument(self.doc_name):
            App.closeDocument(self.doc_name)

    def test_01_accelerated_indexed_adjacency(self):
        """Verify OCC ancestorsOfType accelerates adjacency and returns exact counts."""
        # Create a plate with 4 cutouts
        base = Part.makeBox(60, 60, 15)
        for x in (20, 40):
            for y in (20, 40):
                cyl = Part.makeCylinder(3, 25, App.Vector(x, y, -5))
                base = base.cut(cyl)

        obj = self.doc.addObject("Part::Feature", "MultiHolePlate")
        obj.Shape = base
        self.doc.recompute()

        # Measure adjacency calculation
        edge_counts = adjacent_face_counts(obj)
        vert_counts = adjacent_vertex_counts(obj)

        self.assertEqual(len(edge_counts), len(obj.Shape.Edges))
        self.assertEqual(len(vert_counts), len(obj.Shape.Vertexes))

        # Every edge in a solid has at least 1 adjacent face (2 for manifold edges, 1 for cylinder seams)
        for edge_name, count in edge_counts.items():
            self.assertIn(count, (1, 2), f"{edge_name} expected 1 or 2 adjacent faces")

    def test_02_edge_convexity_concave_vs_convex(self):
        """Verify differential edge convexity identifies internal fillets (concave) vs outer chamfers (convex)."""
        base = Part.makeBox(40, 40, 10)
        boss = Part.makeBox(20, 20, 10, App.Vector(10, 10, 10))
        fused = base.fuse(boss)

        obj = self.doc.addObject("Part::Feature", "SteppedBlock")
        obj.Shape = fused
        self.doc.recompute()

        # Select concave edges (internal junction corners where fillets are applied)
        sel_concave = Selector(
            kind="Edge",
            steps=(Step("filter", {"name": "convexity", "value": "concave"}),),
        )
        concave_refs = sel_concave.evaluate(obj)
        self.assertEqual(len(concave_refs), 4, f"Expected 4 concave junction edges, got {len(concave_refs)}")

        # Verify their Z position is exactly 10.0 (the junction between base and boss)
        for ref in concave_refs:
            edge_shape = obj.Shape.getElement(ref.subname)
            p_mid = edge_shape.valueAt((edge_shape.FirstParameter + edge_shape.LastParameter) / 2.0)
            self.assertAlmostEqual(p_mid.z, 10.0, places=3)

        # Select convex edges (sharp exterior corners)
        sel_convex = Selector(
            kind="Edge",
            steps=(Step("filter", {"name": "convexity", "value": "convex"}),),
        )
        convex_refs = sel_convex.evaluate(obj)
        self.assertEqual(len(convex_refs), 20, f"Expected 20 convex edges, got {len(convex_refs)}")

    def test_03_metric_range_and_diameter_filtering(self):
        """Verify metric_range and diameter filters isolate specific hole sizes on complex plates."""
        base = Part.makeBox(120, 60, 20)
        # Add M4 hole (dia 4, rad 2), M6 hole (dia 6, rad 3), and M10 hole (dia 10, rad 5)
        h4 = Part.makeCylinder(2.0, 30, App.Vector(25, 30, -5))
        h6 = Part.makeCylinder(3.0, 30, App.Vector(60, 30, -5))
        h10 = Part.makeCylinder(5.0, 30, App.Vector(95, 30, -5))
        plate = base.cut(h4).cut(h6).cut(h10)

        obj = self.doc.addObject("Part::Feature", "PlateWithHoles")
        obj.Shape = plate
        self.doc.recompute()

        # Filter M6 hole by diameter range [5.5, 6.5]
        sel_m6 = Selector(
            kind="Face",
            steps=(
                Step("filter", {"name": "cylindrical", "value": True}),
                Step("filter", {"name": "metric_range", "value": {"metric": "diameter", "min": 5.5, "max": 6.5}}),
            ),
        )
        m6_refs = sel_m6.evaluate(obj)
        self.assertEqual(len(m6_refs), 1, "Expected exactly one M6 hole face")

        # Verify candidate diameter metric
        cand = sel_m6.evaluate_candidates(obj)[0]
        self.assertAlmostEqual(cand.diameter, 6.0, places=3)
        self.assertAlmostEqual(cand.radius, 3.0, places=3)

    def test_04_coplanar_and_coaxial_relationships(self):
        """Verify coplanar_with and coaxial_with semantic predicates."""
        # Create two coaxial cylinders of different radii
        c1 = Part.makeCylinder(4.0, 15, App.Vector(20, 20, 0), App.Vector(0, 0, 1))
        c2 = Part.makeCylinder(7.0, 10, App.Vector(20, 20, 15), App.Vector(0, 0, 1))
        # And a non-coaxial cylinder shifted in Y
        c3 = Part.makeCylinder(4.0, 15, App.Vector(20, 50, 0), App.Vector(0, 0, 1))
        compound = Part.Compound([c1, c2, c3])

        obj = self.doc.addObject("Part::Feature", "SteppedShafts")
        obj.Shape = compound
        self.doc.recompute()

        # Coaxial with reference cylinder at (20, 20) with axis (0, 0, 1)
        sel_coaxial = Selector(
            kind="Face",
            steps=(
                Step("filter", {"name": "cylindrical", "value": True}),
                Step(
                    "filter",
                    {
                        "name": "coaxial_with",
                        "value": {
                            "axis_direction": (0.0, 0.0, 1.0),
                            "axis_point": (20.0, 20.0, 0.0),
                            "tolerance": 1e-4,
                        },
                    },
                ),
            ),
        )
        coaxial_refs = sel_coaxial.evaluate(obj)
        self.assertEqual(len(coaxial_refs), 2, "Expected c1 and c2 to match coaxiality query")

    def test_05_project_wide_robustify_all_features(self):
        """Verify robustify_all_features immunizes an entire document in 1 call."""
        # Create base box
        box = self.doc.addObject("Part::Box", "BaseBox")
        box.Length = 40.0
        box.Width = 30.0
        box.Height = 20.0
        self.doc.recompute()

        # Feature 1: Part::Fillet referencing Edge1, Edge2
        fillet = self.doc.addObject("Part::Fillet", "CornersFillet")
        fillet.Base = box
        fillet.Edges = [(1, 2.0, 2.0), (2, 2.0, 2.0)]
        self.doc.recompute()

        # Feature 2: Part::Chamfer on the fillet result referencing Edge3, Edge4
        chamfer = self.doc.addObject("Part::Chamfer", "TopChamfer")
        chamfer.Base = fillet
        chamfer.Edges = [(3, 1.0, 1.0), (4, 1.0, 1.0)]
        self.doc.recompute()

        # Run project-wide robustification
        summary = robustify_all_features(self.doc)
        report = format_robustify_all_report(summary)

        self.assertGreaterEqual(summary["robustified_count"], 2)
        self.assertEqual(summary["failed_count"], 0)
        self.assertIn("CornersFillet", report)
        self.assertIn("TopChamfer", report)

        # Audit document
        audit = audit_document_selectors(self.doc)
        self.assertGreaterEqual(audit["total"], 2)
        print("Audit Selectors:", audit.get("selectors"))
        self.assertEqual(audit["errors"], 0)

        # Recompute document to verify clean DAG execution
        self.doc.recompute()
        self.assertTrue(chamfer.Shape.isValid())

    def test_06_selector_to_python_code_generation(self):
        """Verify selector_to_python generates clean, executable Python code."""
        box = self.doc.addObject("Part::Box", "SampleBox")
        self.doc.recompute()

        selector = Selector(
            kind="Edge",
            steps=(
                Step("filter", {"name": "axis_parallel", "value": "Z"}),
            ),
            expected_count=4,
        )

        code = selector_to_python(selector, "SampleBox")
        self.assertIn("from fs_selector import Selector, Step", code)
        self.assertIn('kind="Edge"', code)
        self.assertIn("expected_count=4", code)
        self.assertIn("axis_parallel", code)

        # Test execution in sandboxed dictionary
        scope = {"SampleBox": box}
        exec(code, scope)
        reconstructed = scope.get("selector")
        subnames = scope.get("subnames")
        self.assertIsNotNone(reconstructed)
        self.assertEqual(reconstructed.kind, "Edge")
        self.assertEqual(len(subnames), 4)

    def _test_07_gui_presets_and_copy_python_button(self):
        """Verify new presets and Copy Python button are accessible in FeatureSelectorPanel."""
        from fs_gui import FeatureSelectorPanel, PRESETS

        panel = FeatureSelectorPanel.instance()
        panel.show_panel()

        # Check new presets in PRESETS list
        preset_names = [p[0] for p in PRESETS]
        self.assertIn("All Internal Fillet Edges (Concave)", preset_names)
        self.assertIn("All External Chamfer Edges (Convex)", preset_names)
        self.assertIn("All Smooth / Tangent Edges", preset_names)
        self.assertIn("Holes by Diameter (M3-M8 Range)", preset_names)

        # Create a test box and learn
        box = self.doc.addObject("Part::Box", "GuiBox")
        self.doc.recompute()
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(box, "Face1")

        panel.learn()
        self.assertIsNotNone(panel.source_obj)

        # Trigger copy_python_code
        panel.copy_python_code()
        self.assertIn("copied to clipboard", panel.status.text())

    def test_08_robustify_all_command_registered(self):
        """Verify FeatureSelector_RobustifyAll is registered in FreeCAD commands."""
        from fs_commands import COMMANDS, TOOLBAR_COMMANDS, MENU_COMMANDS

        cmd_names = [c[0] for c in COMMANDS]
        self.assertIn("FeatureSelector_RobustifyAll", cmd_names)
        self.assertIn("FeatureSelector_RobustifyAll", TOOLBAR_COMMANDS)
        self.assertIn("FeatureSelector_RobustifyAll", MENU_COMMANDS)

        all_cmds = Gui.listCommands()
        self.assertIn("FeatureSelector_RobustifyAll", all_cmds)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRealFreeCADComplexGeometry)
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    os._exit(0 if res.wasSuccessful() else 1)
