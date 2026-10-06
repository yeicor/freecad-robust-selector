"""Real FreeCAD test suite: Geometry and semantic selector primitives.

Executes inside real FreeCAD runtime against actual OCC TopoShapes.
"""
import unittest
import math
try:
    import FreeCAD as App
    import Part
except ImportError:
    App = None

from robust_selector import create, resolve, selector
from fs_selector import Step, candidates


def setUpModule():
    if App is None:
        raise unittest.SkipTest("FreeCAD not available in Python environment")


class TestRealFreeCADPrimitives(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = App.newDocument("TestRealPrimitivesDoc")

    @classmethod
    def tearDownClass(cls):
        App.closeDocument(cls.doc.Name)

    def test_01_box_faces_and_extrema(self):
        """Verify face metrics and extrema on a real Part::Box."""
        box = self.doc.addObject("Part::Box", "Box1")
        box.Length = 100.0
        box.Width = 50.0
        box.Height = 20.0
        self.doc.recompute()

        # Box has 6 planar faces
        face_cands = candidates(box, "Face")
        self.assertEqual(len(face_cands), 6)
        for c in face_cands:
            self.assertEqual(c.geom_type, "PLANE")
            self.assertTrue(c.area > 0)

        # Top face: furthest Z+
        q_top = selector("Face", [
            Step("filter", {"name": "planar", "value": True}),
            Step("extreme", {"metric": "z", "direction": "max"}),
        ], expected_count=1)
        refs = resolve(box, q_top)
        self.assertEqual(len(refs), 1)
        top_face = box.Shape.getElement(refs[0].subname)
        self.assertAlmostEqual(top_face.CenterOfMass.z, 20.0, places=4)
        self.assertAlmostEqual(top_face.Area, 5000.0, places=2)

        # Bottom face: furthest Z-
        q_bottom = selector("Face", [
            Step("filter", {"name": "planar", "value": True}),
            Step("extreme", {"metric": "z", "direction": "min"}),
        ], expected_count=1)
        refs_bot = resolve(box, q_bottom)
        self.assertEqual(len(refs_bot), 1)
        bot_face = box.Shape.getElement(refs_bot[0].subname)
        self.assertAlmostEqual(bot_face.CenterOfMass.z, 0.0, places=4)

    def test_02_cylinder_curved_vs_planar(self):
        """Verify cylinder surface recognition: cylindrical lateral vs planar caps."""
        cyl = self.doc.addObject("Part::Cylinder", "Cyl1")
        cyl.Radius = 15.0
        cyl.Height = 60.0
        self.doc.recompute()

        # Cylindrical lateral face
        q_curved = selector("Face", [
            Step("filter", {"name": "cylindrical", "value": True}),
        ], expected_count=1)
        refs_curved = resolve(cyl, q_curved)
        self.assertEqual(len(refs_curved), 1)
        curved_face = cyl.Shape.getElement(refs_curved[0].subname)
        self.assertAlmostEqual(curved_face.Area, 2 * math.pi * 15.0 * 60.0, places=2)

        # Circular top edge
        q_top_edge = selector("Edge", [
            Step("filter", {"name": "circular", "value": True}),
            Step("extreme", {"metric": "z", "direction": "max"}),
        ], expected_count=1)
        refs_edge = resolve(cyl, q_top_edge)
        self.assertEqual(len(refs_edge), 1)
        top_edge = cyl.Shape.getElement(refs_edge[0].subname)
        self.assertAlmostEqual(top_edge.Length, 2 * math.pi * 15.0, places=2)

    def test_03_sphere_geometry(self):
        """Verify sphere surface classification and surface area calculation."""
        sph = self.doc.addObject("Part::Sphere", "Sph1")
        sph.Radius = 10.0
        self.doc.recompute()

        q_sph = selector("Face", [
            Step("filter", {"name": "spherical", "value": True}),
        ], expected_count=1)
        refs_sph = resolve(sph, q_sph)
        self.assertEqual(len(refs_sph), 1)
        sph_face = sph.Shape.getElement(refs_sph[0].subname)
        self.assertAlmostEqual(sph_face.Area, 4 * math.pi * 100.0, places=2)

    def test_04_box_vertical_edges_multi_selection(self):
        """Verify multi-element semantic selection of all 4 vertical corner edges."""
        box = self.doc.addObject("Part::Box", "BoxCorners")
        box.Length = 40.0
        box.Width = 30.0
        box.Height = 50.0
        self.doc.recompute()

        q_corners = selector("Edge", [
            Step("filter", {"name": "axis_parallel", "value": "Z"}),
        ], expected_count=4)
        refs = resolve(box, q_corners)
        self.assertEqual(len(refs), 4)

        for ref in refs:
            edge = box.Shape.getElement(ref.subname)
            self.assertAlmostEqual(edge.Length, 50.0, places=3)
            # Both vertices have same X and Y
            v0 = edge.Vertexes[0].Point
            v1 = edge.Vertexes[1].Point
            self.assertAlmostEqual(v0.x, v1.x, places=4)
            self.assertAlmostEqual(v0.y, v1.y, places=4)

    def test_05_guardrail_ambiguity_expected_count(self):
        """Verify that when a query is ambiguous, expected_count prevents corruption."""
        box = self.doc.addObject("Part::Box", "BoxGuard")
        box.Length = 10.0
        box.Width = 10.0
        box.Height = 10.0
        self.doc.recompute()

        # Two faces share maximum X center if not filtering by axis
        # Here we deliberately ask for a single face with area == 100, but 6 exist!
        q_ambiguous = selector("Face", [
            Step("filter", {"name": "planar", "value": True}),
        ], expected_count=1)

        raw_cands = q_ambiguous.evaluate_candidates(box)
        self.assertEqual(len(raw_cands), 6)  # 6 match the query

        # resolve() applies the safety guardrail and returns [] when expected_count doesn't match
        refs = resolve(box, q_ambiguous)
        self.assertEqual(len(refs), 0)

        # Persistent selector object reports unresolved status
        robust = create(self.doc, box, q_ambiguous, captured_selection=["Face1"])
        self.assertIn("Unresolved", robust.ResolutionStatus)
        self.assertIn("expected_count_not_satisfied", robust.Resolved)


if __name__ == "__main__":
    unittest.main()
