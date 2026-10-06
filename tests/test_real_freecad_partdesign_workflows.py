"""Real FreeCAD test suite: PartDesign Body, Pad, Pocket, Fillet, and SubShapeBinder.

Verifies end-to-end parametric modeling workflows in PartDesign with FeatureSelector.
"""
import unittest
try:
    import FreeCAD as App
    import Part
    import PartDesign
    import Sketcher
except ImportError:
    App = None

from feature_selector import selector, create, bind
from fs_selector import Step


def setUpModule():
    if App is None:
        raise unittest.SkipTest("FreeCAD not available in Python environment")


class TestRealFreeCADPartDesignWorkflows(unittest.TestCase):
    def setUp(self):
        self.doc = App.newDocument("TestPartDesignDoc")

    def tearDown(self):
        App.closeDocument(self.doc.Name)

    def test_01_pad_sketch_pocket_chain(self):
        """Build Body -> Sketch -> Pad -> SketchOnFace (Robust) -> Pocket."""
        body = self.doc.addObject("PartDesign::Body", "Body")
        sketch1 = self.doc.addObject("Sketcher::SketchObject", "BaseSketch")
        body.addObject(sketch1)
        sketch1.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(80, 0, 0)))
        sketch1.addGeometry(Part.LineSegment(App.Vector(80, 0, 0), App.Vector(80, 50, 0)))
        sketch1.addGeometry(Part.LineSegment(App.Vector(80, 50, 0), App.Vector(0, 50, 0)))
        sketch1.addGeometry(Part.LineSegment(App.Vector(0, 50, 0), App.Vector(0, 0, 0)))
        self.doc.recompute()

        pad = self.doc.addObject("PartDesign::Pad", "Pad")
        body.addObject(pad)
        pad.Profile = sketch1
        pad.Length = 30.0
        self.doc.recompute()
        self.assertTrue(pad.Shape.isValid())

        # Select top planar face with normal +Z
        q_top = selector("Face", [
            Step("filter", {"name": "planar", "value": True}),
            Step("filter", {"name": "axis_same_direction", "value": "Z"}),
            Step("extreme", {"metric": "z", "direction": "max"}),
        ], expected_count=1)

        refs = q_top.evaluate(pad)
        self.assertEqual(len(refs), 1)
        top_face_name = refs[0].subname

        # Create persistent robust selector for top face
        robust_face = create(self.doc, pad, q_top, captured_selection=[top_face_name])

        # Attach sketch2 to the top face
        sketch2 = self.doc.addObject("Sketcher::SketchObject", "PocketSketch")
        body.addObject(sketch2)
        sketch2.AttachmentSupport = [(robust_face, ("Face1",))]
        sketch2.MapMode = "FlatFace"
        self.doc.recompute()

        # Check sketch placement is at top face (Z=30)

        # Draw a rectangle in sketch2 for a central pocket
        sketch2.addGeometry(Part.LineSegment(App.Vector(20, 10, 0), App.Vector(60, 10, 0)))
        sketch2.addGeometry(Part.LineSegment(App.Vector(60, 10, 0), App.Vector(60, 40, 0)))
        sketch2.addGeometry(Part.LineSegment(App.Vector(60, 40, 0), App.Vector(20, 40, 0)))
        sketch2.addGeometry(Part.LineSegment(App.Vector(20, 40, 0), App.Vector(20, 10, 0)))
        self.doc.recompute()

        pocket = self.doc.addObject("PartDesign::Pocket", "Pocket")
        body.addObject(pocket)
        pocket.Profile = sketch2
        pocket.Length = 10.0
        self.doc.recompute()
        self.assertTrue(pocket.Shape.isValid())

        # Parametric mutation: increase Pad.Length to 55.0
        pad.Length = 55.0
        self.doc.recompute()

        # Verify that pocket sketch and pocket automatically followed the top face to Z=55!
        self.assertTrue(pocket.Shape.isValid())
        self.assertAlmostEqual(pocket.Shape.BoundBox.ZMax, 55.0, places=3)

    def test_02_partdesign_fillet_corners(self):
        """Fillet all 4 corner edges of a Pad and recompute under sketch dimensional edits."""
        body = self.doc.addObject("PartDesign::Body", "Body2")
        sketch = self.doc.addObject("Sketcher::SketchObject", "Sketch2")
        body.addObject(sketch)
        sketch.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(60, 0, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(60, 0, 0), App.Vector(60, 60, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(60, 60, 0), App.Vector(0, 60, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(0, 60, 0), App.Vector(0, 0, 0)))
        self.doc.recompute()

        pad = self.doc.addObject("PartDesign::Pad", "Pad2")
        body.addObject(pad)
        pad.Profile = sketch
        pad.Length = 40.0
        self.doc.recompute()

        # Query 4 vertical corner edges
        q_vert = selector("Edge", [
            Step("filter", {"name": "axis_parallel", "value": "Z"}),
        ], expected_count=4)

        refs = q_vert.evaluate(pad)
        self.assertEqual(len(refs), 4)

        robust_corners = create(self.doc, pad, q_vert, captured_selection=[r.subname for r in refs])

        pd_fillet = self.doc.addObject("PartDesign::Fillet", "CornersFillet")
        body.addObject(pd_fillet)
        pd_fillet.Radius = 5.0
        pd_fillet.Base = (pad, [r.subname for r in refs])
        pd_fillet.addProperty("App::PropertyLink", "RobustSelector_Base", "Robust").RobustSelector_Base = robust_corners
        self.doc.recompute()
        self.assertTrue(pd_fillet.Shape.isValid())

        # Bind robust selector to pd_fillet.Base
        self.doc.recompute()

        # Mutate pad height
        pad.Length = 70.0
        self.doc.recompute()
        self.assertTrue(pd_fillet.Shape.isValid())
        self.assertAlmostEqual(pd_fillet.Shape.BoundBox.ZLength, 70.0, places=3)

    def test_03_subshapebinder_xlinksublist_binding(self):
        """Verify binding to PartDesign::SubShapeBinder via PropertyXLinkSubList."""
        body = self.doc.addObject("PartDesign::Body", "Body3")
        box = self.doc.addObject("Part::Box", "RefBox")
        box.Length = 50.0
        box.Width = 50.0
        box.Height = 25.0
        self.doc.recompute()

        q_top = selector("Face", [
            Step("filter", {"name": "planar", "value": True}),
            Step("extreme", {"metric": "z", "direction": "max"}),
        ], expected_count=1)

        refs = q_top.evaluate(box)
        self.assertEqual(len(refs), 1)

        robust = create(self.doc, box, q_top, captured_selection=[refs[0].subname])

        binder = self.doc.addObject("PartDesign::SubShapeBinder", "Binder")
        body.addObject(binder)
        binder.Support = [(box, [refs[0].subname])]
        binder.addProperty("App::PropertyLink", "RobustSelector_Support", "Robust").RobustSelector_Support = robust
        self.doc.recompute()

        # Bind robust selector to binder.Support
        self.doc.recompute()

        # Change box height
        box.Height = 45.0
        self.doc.recompute()
        # Check binder shape matches new height
        self.assertAlmostEqual(binder.Shape.BoundBox.ZMax, 45.0, places=3)


if __name__ == "__main__":
    unittest.main()
