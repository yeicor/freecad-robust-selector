"""Unit tests for CadQuery expression language and cursor autocompletion in FreeCAD."""
import unittest
import os
import time

try:
    import FreeCAD as App
    import Part
except ImportError:
    App = None
    Part = None

from fs_expression import (
    evaluate_expression,
    evaluate_expression_items,
    parse_expression,
    autocomplete_at_cursor,
    steps_to_expression,
)
from fs_selector import Selector


class TestCadQueryExpressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if App is None:
            raise unittest.SkipTest("FreeCAD not available")
        cls.doc = App.newDocument("DocCadQueryTests")
        cls.box = cls.doc.addObject("Part::Box", "TestBox")
        cls.box.Length = 40.0
        cls.box.Width = 30.0
        cls.box.Height = 20.0

        cls.cyl = cls.doc.addObject("Part::Cylinder", "TestCyl")
        cls.cyl.Radius = 10.0
        cls.cyl.Height = 25.0
        cls.doc.recompute()

    @classmethod
    def tearDownClass(cls):
        if App is not None and hasattr(cls, "doc"):
            App.closeDocument(cls.doc.Name)

    def test_01_extrema_and_directional_selectors(self):
        """Verify >Z, <Z, >X, <X, >Y, <Y on a Part::Box."""
        top_faces = evaluate_expression(self.box, ">Z", kind="Face")
        self.assertEqual(len(top_faces), 1)
        self.assertEqual(top_faces[0].subname, "Face6")

        bot_faces = evaluate_expression(self.box, "<Z", kind="Face")
        self.assertEqual(len(bot_faces), 1)
        self.assertEqual(bot_faces[0].subname, "Face5")

        right_faces = evaluate_expression(self.box, ">X", kind="Face")
        self.assertEqual(len(right_faces), 1)
        self.assertEqual(right_faces[0].subname, "Face2")

        left_faces = evaluate_expression(self.box, "<X", kind="Face")
        self.assertEqual(len(left_faces), 1)
        self.assertEqual(left_faces[0].subname, "Face1")

    def test_02_parallel_and_perpendicular_filters(self):
        """Verify |Z (parallel) and #Z (perpendicular) for edges and faces."""
        vert_edges = evaluate_expression(self.box, "|Z", kind="Edge")
        self.assertEqual(len(vert_edges), 4)

        horiz_edges = evaluate_expression(self.box, "#Z", kind="Edge")
        self.assertEqual(len(horiz_edges), 8)

    def test_03_geometry_type_filters(self):
        """Verify %Plane and %Cylinder filters."""
        planar_faces = evaluate_expression(self.cyl, "%Plane", kind="Face")
        self.assertEqual(len(planar_faces), 2)  # top and bottom circular caps

        curved_faces = evaluate_expression(self.cyl, "%Cylinder", kind="Face")
        self.assertEqual(len(curved_faces), 1)  # lateral cylinder surface

    def test_04_logical_combinations(self):
        """Verify and, or, not, exc combinations."""
        # Top or bottom faces
        caps = evaluate_expression(self.box, ">Z or <Z", kind="Face")
        self.assertEqual(len(caps), 2)
        subnames = {f.subname for f in caps}
        self.assertEqual(subnames, {"Face5", "Face6"})

        # Not top face
        non_top = evaluate_expression(self.box, "not >Z", kind="Face")
        self.assertEqual(len(non_top), 5)

        # Difference: all faces except top face
        diff = evaluate_expression(self.box, "%Plane exc >Z", kind="Face")
        self.assertEqual(len(diff), 5)
        self.assertNotIn("Face6", {f.subname for f in diff})

    def test_05_descending_component_pipeline(self):
        """Verify faces('>Z').edges() and faces('>Z').edges('<X')."""
        # All edges of the top face
        top_edges = evaluate_expression(self.box, 'faces(">Z").edges()')
        self.assertEqual(len(top_edges), 4)

        # Single edge: edge of top face with min X
        left_top_edge = evaluate_expression(self.box, 'faces(">Z").edges("<X")')
        self.assertEqual(len(left_top_edge), 1)

        # Vertices of the top face
        top_verts = evaluate_expression(self.box, 'faces(">Z").vertices()')
        self.assertEqual(len(top_verts), 4)

        # Shorthand without quotes: faces(>Z).edges(<X)
        shorthand = evaluate_expression(self.box, "faces(>Z).edges(<X)")
        self.assertEqual(len(shorthand), 1)
        self.assertEqual(shorthand[0].subname, left_top_edge[0].subname)

    def test_06_autocomplete_at_cursor(self):
        """Verify rapid cursor autocompletion finds exact and narrowing selectors."""
        # Target: top face (Face6)
        text, offset = autocomplete_at_cursor(self.box, ["Face6"], "Face", "", 0)
        self.assertEqual(text, ">Z")

        # Target: inside faces() call
        text, offset = autocomplete_at_cursor(self.box, ["Face6"], "Face", "faces(", 6)
        self.assertEqual(text, '">Z")')

        # Descending target: left edge on top face
        left_top_subname = evaluate_expression(self.box, 'faces(">Z").edges("<X")')[0].subname
        text, offset = autocomplete_at_cursor(self.box, [left_top_subname], "Edge", "", 0)
        self.assertIn("faces", text)
        self.assertIn("edges", text)

        # Performance: autocomplete must be fast (<50ms)
        t0 = time.perf_counter()
        autocomplete_at_cursor(self.box, ["Face6"], "Face", "", 0)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertTrue(elapsed_ms < 50.0, f"Autocomplete took {elapsed_ms:.2f}ms, expected < 50ms")


if __name__ == "__main__":
    unittest.main()
