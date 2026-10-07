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

        # Stepped block for convexity (:concave, :convex)
        base_step = Part.makeBox(60, 60, 10)
        boss_step = Part.makeBox(30, 30, 15, App.Vector(15, 15, 10))
        cls.stepped = cls.doc.addObject("Part::Feature", "TestStepped")
        cls.stepped.Shape = base_step.fuse(boss_step)

        # Plate with holes of radii 2.0, 5.0, 10.0 for metric clustering, comparisons & ranges
        b_plate = Part.makeBox(120, 60, 20)
        c2 = Part.makeCylinder(2.0, 30, App.Vector(25, 30, -5))
        c5 = Part.makeCylinder(5.0, 30, App.Vector(60, 30, -5))
        c10 = Part.makeCylinder(10.0, 30, App.Vector(95, 30, -5))
        cls.plate = cls.doc.addObject("Part::Feature", "TestPlate")
        cls.plate.Shape = b_plate.cut(c2).cut(c5).cut(c10)

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

    def test_07_canonical_metric_clustering(self):
        """Verify >>metric[sel] descending and <<metric[sel] ascending clustering."""
        # Largest radius (r=10) on plate
        largest_rad = evaluate_expression_items(self.plate, ">>radius[0]", kind="Face")
        self.assertEqual(len(largest_rad), 1)
        self.assertAlmostEqual(largest_rad[0].radius, 10.0, places=2)

        # Smallest radius (r=2) on plate
        smallest_rad = evaluate_expression_items(self.plate, "<<radius[0]", kind="Face")
        self.assertEqual(len(smallest_rad), 1)
        self.assertAlmostEqual(smallest_rad[0].radius, 2.0, places=2)

        # Slice indexing: top 2 radius clusters (r=10 and r=5)
        top_two_rad = evaluate_expression_items(self.plate, ">>radius[0:2]", kind="Face")
        self.assertEqual(len(top_two_rad), 2)
        rad_vals = sorted([r.radius for r in top_two_rad], reverse=True)
        self.assertAlmostEqual(rad_vals[0], 10.0, places=2)
        self.assertAlmostEqual(rad_vals[1], 5.0, places=2)

        # Cluster group predicates: unique and largest
        unique_rad = evaluate_expression(self.plate, ">>radius[unique]", kind="Face")
        self.assertEqual(len(unique_rad), 3)

        # Longest edge on box
        longest_edges = evaluate_expression_items(self.box, ">>length[0]", kind="Edge")
        self.assertEqual(len(longest_edges), 4)  # 4 edges of length 40
        self.assertAlmostEqual(longest_edges[0].length, 40.0, places=2)

        # Coordinate clustering: >>Z[0] (max Z) and <<Z[0] (min Z)
        max_z_faces = evaluate_expression(self.box, ">>Z[0]", kind="Face")
        self.assertEqual(len(max_z_faces), 1)
        self.assertEqual(max_z_faces[0].subname, "Face6")

        min_z_faces = evaluate_expression(self.box, "<<Z[0]", kind="Face")
        self.assertEqual(len(min_z_faces), 1)
        self.assertEqual(min_z_faces[0].subname, "Face5")

    def test_08_topological_tags(self):
        """Verify tag filters: :concave, :convex, :circular, :linear, :planar, :cylindrical."""
        # Concave internal junction edges on stepped block
        concave_edges = evaluate_expression_items(self.stepped, ":concave", kind="Edge")
        self.assertEqual(len(concave_edges), 4)
        for e in concave_edges:
            self.assertEqual(e.convexity, "concave")

        # Convex external chamfer edges on stepped block
        convex_edges = evaluate_expression_items(self.stepped, ":convex", kind="Edge")
        self.assertEqual(len(convex_edges), 20)
        for e in convex_edges:
            self.assertEqual(e.convexity, "convex")

        # Circular edges of holes on plate (3 holes * 2 circular edges = 6)
        circ_edges = evaluate_expression(self.plate, ":circular", kind="Edge")
        self.assertEqual(len(circ_edges), 6)

        # Linear edges of plate (12 outer box edges + 3 cylinder seams = 15)
        linear_edges = evaluate_expression(self.plate, ":linear", kind="Edge")
        self.assertEqual(len(linear_edges), 15)
        box_linear = evaluate_expression(self.box, ":linear", kind="Edge")
        self.assertEqual(len(box_linear), 12)

        # Cylindrical faces of holes on plate
        cyl_faces = evaluate_expression(self.plate, ":cylindrical", kind="Face")
        self.assertEqual(len(cyl_faces), 3)

        # Planar faces of plate (6 box faces)
        planar_faces = evaluate_expression(self.plate, ":planar", kind="Face")
        self.assertEqual(len(planar_faces), 6)

    def test_09_numeric_comparisons_and_ranges(self):
        """Verify metric == val, metric > val, and min <= metric <= max ranges."""
        # Equality: radius == 5.0
        r5_faces = evaluate_expression_items(self.plate, "radius == 5.0", kind="Face")
        self.assertEqual(len(r5_faces), 1)
        self.assertAlmostEqual(r5_faces[0].radius, 5.0, places=2)

        # Greater than: radius > 3.0
        r_gt3 = evaluate_expression(self.plate, "radius > 3.0", kind="Face")
        self.assertEqual(len(r_gt3), 2)  # radii 5.0 and 10.0

        # Chained range: 3.0 <= diameter <= 15.0
        mid_holes = evaluate_expression(self.plate, "3.0 <= diameter <= 15.0", kind="Face")
        self.assertEqual(len(mid_holes), 2)  # dia 4.0 (r=2) and dia 10.0 (r=5)

        # Python 'in [min, max]' range
        in_range = evaluate_expression(self.plate, "radius in [1.5, 6.0]", kind="Face")
        self.assertEqual(len(in_range), 2)  # radii 2.0 and 5.0

    def test_10_relational_combinators(self):
        """Verify adjacent_to, coplanar_to, and coaxial_to relational filters."""
        # Faces adjacent to Face6 (top face of box has 4 adjacent walls)
        adj_faces = evaluate_expression(self.box, 'adjacent_to("Face6")', kind="Face")
        self.assertEqual(len(adj_faces), 4)
        self.assertNotIn("Face6", [f.subname for f in adj_faces])

        # Edges adjacent to Face6 (all 8 edges touching Face6: 4 boundary edges + 4 vertical pillars)
        adj_edges = evaluate_expression(self.box, 'adjacent_to("Face6")', kind="Edge")
        self.assertEqual(len(adj_edges), 8)

        # Coplanar faces to Face6
        coplanar_faces = evaluate_expression(self.box, 'coplanar_to("Face6")', kind="Face")
        self.assertEqual(len(coplanar_faces), 1)
        self.assertEqual(coplanar_faces[0].subname, "Face6")

    def test_11_tolerance_parameter_effect(self):
        """Verify tolerance controls cluster epsilon and numeric float matching."""
        b = Part.makeBox(100, 50, 20)
        c1 = Part.makeCylinder(5.0000, 30, App.Vector(25, 25, -5))
        c2 = Part.makeCylinder(5.0005, 30, App.Vector(75, 25, -5))
        tol_obj = self.doc.addObject("Part::Feature", "TolObj")
        tol_obj.Shape = b.cut(c1).cut(c2)
        self.doc.recompute()

        # Strict tolerance (1e-4): cylinders form 2 distinct clusters
        strict = evaluate_expression(tol_obj, ">>radius[0]", kind="Face", tolerance=1e-4)
        self.assertEqual(len(strict), 1)

        # Relaxed tolerance (1e-2): cylinders merge into 1 cluster
        relaxed = evaluate_expression(tol_obj, ">>radius[0]", kind="Face", tolerance=1e-2)
        self.assertEqual(len(relaxed), 2)

    def test_12_expression_editor_tab_completion_and_highlighting(self):
        """Verify ExpressionEditor Tab-completion and ExpressionHighlighter formatting."""
        try:
            from PySide6 import QtCore, QtGui, QtWidgets
            from fs_gui import ExpressionEditor, ExpressionHighlighter
        except ImportError:
            raise unittest.SkipTest("PySide6 GUI not available")

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        editor = ExpressionEditor()
        highlighter = ExpressionHighlighter(editor.document())

        tab_event = QtGui.QKeyEvent(
            QtCore.QEvent.Type.KeyPress,
            QtCore.Qt.Key.Key_Tab,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )

        # 1. Expand tag abbreviation: :con -> :concave
        editor.setPlainText(":con")
        cursor = editor.textCursor()
        cursor.setPosition(4)
        editor.setTextCursor(cursor)
        editor.keyPressEvent(tab_event)
        self.assertEqual(editor.toPlainText(), ":concave")

        # 2. Expand cluster abbreviation: >>rad -> >>radius[0]
        editor.setPlainText(">>rad")
        cursor = editor.textCursor()
        cursor.setPosition(5)
        editor.setTextCursor(cursor)
        editor.keyPressEvent(tab_event)
        self.assertEqual(editor.toPlainText(), ">>radius[0]")

        # 3. Expand cluster abbreviation: <<rad -> <<radius[0]
        editor.setPlainText("<<rad")
        cursor = editor.textCursor()
        cursor.setPosition(5)
        editor.setTextCursor(cursor)
        editor.keyPressEvent(tab_event)
        self.assertEqual(editor.toPlainText(), "<<radius[0]")

        # 4. Expand method abbreviation: fac -> faces("
        editor.setPlainText("fac")
        cursor = editor.textCursor()
        cursor.setPosition(3)
        editor.setTextCursor(cursor)
        editor.keyPressEvent(tab_event)
        self.assertEqual(editor.toPlainText(), 'faces("')

        # 5. Verify highlighter runs without errors on full expression
        editor.setPlainText('faces(">Z").edges(":concave") and >>radius[0]')
        highlighter.rehighlight()
        self.assertFalse(editor.document().isEmpty())

    def test_13_conditional_clustering_and_seam_tags(self):
        """Verify conditional clustering selectors (EXP-2) and seam/hole tags (EXP-3)."""
        # Test seam on cylinder
        cyl = self.doc.addObject("Part::Feature", "Cyl")
        cyl.Shape = Part.makeCylinder(10.0, 30.0)
        self.doc.recompute()
        seam_edges = evaluate_expression(cyl, ":seam", kind="Edge")
        self.assertEqual(len(seam_edges), 1)

        # Test hole tag on plate with drilled hole
        plate = self.doc.addObject("Part::Feature", "PlateHole")
        b = Part.makeBox(50, 50, 10)
        hole = Part.makeCylinder(5, 20, App.Vector(25, 25, -5))
        plate.Shape = b.cut(hole)
        self.doc.recompute()

        # Face with hole
        hole_faces = evaluate_expression(plate, ":hole", kind="Face")
        self.assertTrue(len(hole_faces) >= 1)

        # Edge of hole
        hole_edges = evaluate_expression(plate, ":hole", kind="Edge")
        self.assertEqual(len(hole_edges), 2)  # Top and bottom circles of the hole

        # Conditional clustering: >>length[!=0]
        edges_nonzero = evaluate_expression(plate, ">>length[!=0]", kind="Edge")
        self.assertEqual(len(edges_nonzero), len(plate.Shape.Edges))

    def test_14_freecad_parametric_expressions(self):
        """Verify integration with FreeCAD parametric expressions (=VarSet.param, arithmetic, units)."""
        varset = self.doc.addObject("App::VarSet", "ParamVarSet")
        varset.addProperty("App::PropertyLength", "drill_r")
        varset.drill_r = 5.0
        varset.addProperty("App::PropertyLength", "min_r")
        varset.min_r = 3.0
        varset.addProperty("App::PropertyLength", "max_r")
        varset.max_r = 8.0
        varset.addProperty("App::PropertyInteger", "target_tier")
        varset.target_tier = 0
        self.doc.recompute()

        # 1. Compare equality with leading '=': radius == =ParamVarSet.drill_r
        res_eq = evaluate_expression(self.plate, "radius == =ParamVarSet.drill_r", kind="Face")
        self.assertEqual(len(res_eq), 1)

        # 2. Compare equality with bare dotted name: radius == ParamVarSet.drill_r
        res_dotted = evaluate_expression(self.plate, "radius == ParamVarSet.drill_r", kind="Face")
        self.assertEqual(len(res_dotted), 1)
        self.assertEqual(res_eq[0].subname, res_dotted[0].subname)

        # 3. Numeric range with expressions: =ParamVarSet.min_r <= radius <= =ParamVarSet.max_r
        res_range = evaluate_expression(self.plate, "=ParamVarSet.min_r <= radius <= =ParamVarSet.max_r", kind="Face")
        self.assertEqual(len(res_range), 1)

        # 4. Arithmetic expression: radius == =ParamVarSet.drill_r * 2 (matches 10.0 hole)
        res_arith = evaluate_expression(self.plate, "radius == =ParamVarSet.drill_r * 2", kind="Face")
        self.assertEqual(len(res_arith), 1)
        self.assertNotEqual(res_eq[0].subname, res_arith[0].subname)

        # 5. Cluster bracket expression: >>radius[=ParamVarSet.drill_r]
        res_cluster = evaluate_expression(self.plate, ">>radius[=ParamVarSet.drill_r]", kind="Face")
        self.assertEqual(len(res_cluster), 1)
        self.assertEqual(res_cluster[0].subname, res_eq[0].subname)

        # 6. Cluster condition with expression: >>radius[!= =ParamVarSet.drill_r]
        res_neq = evaluate_expression(self.plate, ">>radius[!= =ParamVarSet.drill_r]", kind="Face")
        self.assertEqual(len(res_neq), 2)  # holes with r=2 and r=10

    def test_15_synchronized_palette_and_clause_decomposition(self):
        """Verify decomposition of clauses and 1:1 color synchronization with 3D elements."""
        from fs_expression import (
            decompose_clauses,
            compute_synchronized_colors,
            SYNCHRONIZED_PALETTE,
            OVERLAP_COLOR,
        )

        # 1. Decompose compound union expression
        clauses = decompose_clauses(">Z | <Z")
        self.assertEqual(len(clauses), 2)
        self.assertEqual(clauses[0].text, ">Z")
        self.assertEqual(clauses[0].color_index, 0)
        self.assertEqual(clauses[1].text, "<Z")
        self.assertEqual(clauses[1].color_index, 1)

        # 2. Decompose wrapped method expression: faces(">Z, <Z, |X")
        clauses_wrap = decompose_clauses('faces(">Z, <Z, |X")')
        self.assertEqual(len(clauses_wrap), 3)
        self.assertEqual(clauses_wrap[0].text, ">Z")
        self.assertEqual(clauses_wrap[1].text, "<Z")
        self.assertEqual(clauses_wrap[2].text, "|X")

        # 3. Compute synchronized 3D colors on box
        res_clauses, color_map = compute_synchronized_colors(self.box, ">Z | <Z", kind="Face")
        self.assertEqual(len(res_clauses), 2)
        self.assertIn("Face6", color_map)
        self.assertIn("Face5", color_map)
        # Face6 matches Clause 0 (Cyan)
        self.assertEqual(color_map["Face6"], SYNCHRONIZED_PALETTE[0]["rgba_3d"])
        # Face5 matches Clause 1 (Amber)
        self.assertEqual(color_map["Face5"], SYNCHRONIZED_PALETTE[1]["rgba_3d"])

        # 4. Overlap / intersection receives emerald overlap color
        res_clauses2, color_map2 = compute_synchronized_colors(
            self.box, "%Plane and >Z", kind="Face"
        )
        self.assertIn("Face6", color_map2)
        self.assertEqual(color_map2["Face6"], OVERLAP_COLOR["rgba_3d"])

        # 5. Active clause focus accentuates the active clause's elements
        _, color_focus = compute_synchronized_colors(
            self.box, ">Z | <Z", kind="Face", active_clause_idx=1
        )
        self.assertEqual(color_focus["Face5"], SYNCHRONIZED_PALETTE[1]["rgba_3d"])


if __name__ == "__main__":
    unittest.main()


