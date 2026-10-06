"""Real FreeCAD regression test: no false-positive errors across a topology shock.

Builds Body -> InvoluteGear -> SketchOnTop -> Pocket, robustifies the sketch
attachment, then toggles ``InvoluteGear.simple`` (92 faces <-> 3 faces).
The sketch attaches to the selector's stable proxy face, so it shares no
direct link with the gear: a source-only recompute must never mark it
Invalid/Error, and a full recompute must land clean with correct geometry.
"""
import unittest

try:
    import FreeCAD as App
    import Part
except ImportError:
    App = None

try:
    from freecad.gears.involutegear import InvoluteGear
except ImportError:
    InvoluteGear = None


def setUpModule():
    if App is None:
        raise unittest.SkipTest("FreeCAD not available in Python environment")
    if InvoluteGear is None:
        raise unittest.SkipTest("freecad.gears not available in Python environment")


class TestGearTopologyShock(unittest.TestCase):
    def setUp(self):
        from fs_bindings import robustify_feature

        self.doc = App.newDocument("TestGearShockDoc")
        body = self.doc.addObject("PartDesign::Body", "Body")
        gear = self.doc.addObject("PartDesign::FeaturePython", "InvoluteGear")
        InvoluteGear(gear)
        body.addObject(gear)
        self.doc.recompute()
        self.assertEqual(len(gear.Shape.Faces), 92)

        sketch = self.doc.addObject("Sketcher::SketchObject", "Sketch")
        body.addObject(sketch)
        top = next(
            "Face%d" % (i + 1)
            for i, face in enumerate(gear.Shape.Faces)
            if abs(face.BoundBox.ZMin - gear.Shape.BoundBox.ZMax) < 1e-6
        )
        sketch.AttachmentSupport = [(gear, (top,))]
        sketch.MapMode = "FlatFace"
        sketch.addGeometry(Part.LineSegment(App.Vector(-2, -2, 0), App.Vector(2, -2, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(2, -2, 0), App.Vector(2, 2, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(2, 2, 0), App.Vector(-2, 2, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(-2, 2, 0), App.Vector(-2, -2, 0)))
        pocket = self.doc.addObject("PartDesign::Pocket", "Pocket")
        body.addObject(pocket)
        pocket.Profile = sketch
        pocket.Length = 2.0
        self.doc.recompute()

        selector, _plan = robustify_feature(pocket)
        self.body = body
        self.gear = gear
        self.sketch = sketch
        self.pocket = pocket
        self.selector = selector

    def tearDown(self):
        App.closeDocument(self.doc.Name)

    def _assert_no_false_errors(self, tag):
        for obj in (self.gear, self.selector, self.sketch, self.pocket, self.body):
            self.assertNotIn("Invalid", obj.State, f"{tag}: {obj.Name} Invalid: {obj.State}")
            self.assertNotIn("Error", obj.State, f"{tag}: {obj.Name} Error: {obj.State}")

    def _toggle_and_verify(self, simple):
        support = [(o.Name, tuple(n)) for o, n in self.sketch.AttachmentSupport]
        self.assertEqual(support, [("FeatureSelector", ("Face1",))])

        self.gear.simple = simple
        self.doc.recompute([self.gear])
        # The shock must not even momentarily invalidate the sketch: it shares
        # no direct link with the gear anymore.
        self._assert_no_false_errors(f"source-only pass (simple={simple})")
        support = [(o.Name, tuple(n)) for o, n in self.sketch.AttachmentSupport]
        self.assertEqual(support, [("FeatureSelector", ("Face1",))])

        self.doc.recompute()
        self._assert_no_false_errors(f"full pass (simple={simple})")
        for obj in (self.gear, self.selector, self.sketch, self.pocket, self.body):
            self.assertEqual(obj.State, ["Up-to-date"], f"{obj.Name}: {obj.State}")
        self.assertTrue(self.pocket.Shape.isValid())
        self.assertAlmostEqual(
            self.sketch.Placement.Base.z, self.gear.Shape.BoundBox.ZMax, places=3
        )

    def test_01_collapse_92_to_3_faces(self):
        """Full gear -> simple gear: no false errors, geometry follows."""
        self._toggle_and_verify(True)
        self.assertEqual(len(self.gear.Shape.Faces), 3)

    def test_02_restore_3_to_92_faces(self):
        """Simple gear -> full gear: no false errors, geometry follows."""
        self.gear.simple = True
        self.doc.recompute()
        self._toggle_and_verify(False)
        self.assertEqual(len(self.gear.Shape.Faces), 92)


if __name__ == "__main__":
    unittest.main()
