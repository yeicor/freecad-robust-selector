"""Real FreeCAD test suite: robust selectors across CAD utilities.

Verifies the explicit binding workflow against the consumers FreeCAD users
actually combine: Part dress-ups (Fillet/Chamfer/Thickness), PartDesign
Draft, datum planes, Assembly joints (XLinkSub face references), and
TechDraw dimensions.  Each test robustifies a real native reference, applies
a parametric mutation, and requires a single recompute to land clean with no
false-positive errors.  Boundaries (ambiguous intent after a topology shock)
are asserted as honest, visible Warnings with the native value untouched.
"""
import unittest

try:
    import FreeCAD as App
    import Part
except ImportError:
    App = None


def _assembly_joint_class():
    """Import the Assembly joint helper lazily: JointObject pulls Qt/Coin at
    module import, which must happen after the GUI is up, never at collection."""
    import sys
    sys.path.insert(0, "/usr/lib/freecad/Mod/Assembly")
    import UtilsAssembly
    from JointObject import Joint
    return UtilsAssembly, Joint


def setUpModule():
    if App is None:
        raise unittest.SkipTest("FreeCAD not available in Python environment")


def _pad_from_rect(doc, body_name="Body", sketch_name="S", pad_name="P",
                   width=60.0, depth=40.0, length=10.0):
    body = doc.addObject("PartDesign::Body", body_name)
    sketch = doc.addObject("Sketcher::SketchObject", sketch_name)
    body.addObject(sketch)
    sketch.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(width, 0, 0)))
    sketch.addGeometry(Part.LineSegment(App.Vector(width, 0, 0), App.Vector(width, depth, 0)))
    sketch.addGeometry(Part.LineSegment(App.Vector(width, depth, 0), App.Vector(0, depth, 0)))
    sketch.addGeometry(Part.LineSegment(App.Vector(0, depth, 0), App.Vector(0, 0, 0)))
    pad = doc.addObject("PartDesign::Pad", pad_name)
    body.addObject(pad)
    pad.Profile = sketch
    pad.Length = length
    doc.recompute()
    return body, sketch, pad


def _assert_healthy(testcase, objects, tag):
    for obj in objects:
        testcase.assertNotIn("Invalid", obj.State, f"{tag}: {obj.Name} Invalid: {obj.State}")
        testcase.assertNotIn("Error", obj.State, f"{tag}: {obj.Name} Error: {obj.State}")


class TestConsumerCompatibility(unittest.TestCase):
    def setUp(self):
        self.doc = App.newDocument("TestConsumerCompatDoc")

    def tearDown(self):
        App.closeDocument(self.doc.Name)

    def test_01_part_fillet_edges_follow_parametric_change(self):
        """Part::Fillet (PropertyFilletEdges) survives a parametric mutation."""
        from fs_bindings import robustify_feature

        box = self.doc.addObject("Part::Box", "Box")
        box.Length, box.Width, box.Height = 20.0, 20.0, 10.0
        self.doc.recompute()
        fillet = self.doc.addObject("Part::Fillet", "Fillet")
        fillet.Base = box
        fillet.Edges = [(1, 2.0, 2.0), (2, 2.0, 2.0)]
        self.doc.recompute()
        self.assertTrue(fillet.Shape.isValid())

        selector, _plan = robustify_feature(fillet, self.doc, prop_name="Edges")
        self.assertEqual(selector.HealthStatus, "OK")
        self.assertTrue(selector.BindingStatus.startswith("Bound explicitly"))

        box.Height = 25.0
        self.doc.recompute()
        _assert_healthy(self, [box, fillet, selector], "after Height change")
        for obj in (box, fillet, selector):
            self.assertEqual(obj.State, ["Up-to-date"], f"{obj.Name}: {obj.State}")
        self.assertTrue(fillet.Shape.isValid())
        self.assertAlmostEqual(fillet.Shape.BoundBox.ZLength, 25.0, places=3)

    def test_02_part_chamfer_edges_follow_parametric_change(self):
        """Part::Chamfer (PropertyFilletEdges) survives a parametric mutation."""
        from fs_bindings import robustify_feature

        box = self.doc.addObject("Part::Box", "Box")
        box.Length, box.Width, box.Height = 20.0, 20.0, 10.0
        self.doc.recompute()
        chamfer = self.doc.addObject("Part::Chamfer", "Chamfer")
        chamfer.Base = box
        chamfer.Edges = [(1, 1.0, 1.0)]
        self.doc.recompute()
        self.assertTrue(chamfer.Shape.isValid())

        robustify_feature(chamfer, self.doc, prop_name="Edges")
        box.Height = 30.0
        self.doc.recompute()
        _assert_healthy(self, [box, chamfer], "after Height change")
        self.assertEqual(chamfer.State, ["Up-to-date"])
        self.assertTrue(chamfer.Shape.isValid())
        self.assertAlmostEqual(chamfer.Shape.BoundBox.ZLength, 30.0, places=3)

    def test_03_part_thickness_faces_follow_parametric_change(self):
        """Part::Thickness (LinkSub Faces) survives a parametric mutation."""
        from fs_bindings import robustify_feature

        box = self.doc.addObject("Part::Box", "Box")
        box.Length, box.Width, box.Height = 20.0, 20.0, 10.0
        self.doc.recompute()
        thick = self.doc.addObject("Part::Thickness", "Thick")
        thick.Faces = (box, ["Face1"])
        thick.Value = 1.0
        self.doc.recompute()
        self.assertTrue(thick.Shape.isValid())

        robustify_feature(thick, self.doc, prop_name="Faces")
        box.Height = 30.0
        self.doc.recompute()
        _assert_healthy(self, [box, thick], "after Height change")
        self.assertTrue(thick.Shape.isValid())
        self.assertAlmostEqual(thick.Shape.BoundBox.ZLength, box.Height.Value + 2.0, places=3)

    def test_04_partdesign_draft_base_follows_parametric_change(self):
        """PartDesign::Draft (LinkSub Base) survives a parametric mutation."""
        from fs_bindings import robustify_feature

        body, _sketch, pad = _pad_from_rect(self.doc, width=20.0, depth=10.0)
        draft = self.doc.addObject("PartDesign::Draft", "Draft")
        body.addObject(draft)
        draft.Base = (pad, ["Face1", "Face2", "Face3", "Face4"])
        draft.NeutralPlane = (pad, ["Face5"])
        self.doc.recompute()
        self.assertEqual(draft.State, ["Up-to-date"])

        robustify_feature(draft, self.doc, prop_name="Base")
        pad.Length = 20.0
        self.doc.recompute()
        _assert_healthy(self, [pad, draft], "after Length change")
        self.assertEqual(draft.State, ["Up-to-date"])

    def test_05_datum_plane_uses_stable_proxy_support(self):
        """Datum planes attach to the selector proxy and follow the source."""
        from fs_bindings import robustify_feature

        body, _sketch, pad = _pad_from_rect(self.doc, width=20.0, depth=10.0)
        plane = self.doc.addObject("PartDesign::Plane", "DatumPlane")
        body.addObject(plane)
        plane.AttachmentSupport = [(pad, ["Face6"])]
        plane.MapMode = "FlatFace"
        self.doc.recompute()

        selector, _plan = robustify_feature(plane, self.doc)
        support = [(o.Name, tuple(n)) for o, n in plane.AttachmentSupport]
        self.assertEqual(support, [(selector.Name, ("Face1",))])

        pad.Length = 35.0
        self.doc.recompute()
        _assert_healthy(self, [pad, plane, selector], "after Length change")
        self.assertAlmostEqual(plane.Placement.Base.z, 35.0, places=3)

    def test_06_assembly_fixed_joint_references_survive_mutation(self):
        """Assembly joint XLinkSub face references survive part mutation."""
        try:
            UtilsAssembly, Joint = _assembly_joint_class()
        except ImportError:
            self.skipTest("Assembly workbench not available")
        from fs_bindings import robustify_feature

        asm = self.doc.addObject("Assembly::AssemblyObject", "Asm")
        joint_group = UtilsAssembly.getJointGroup(asm)
        box1 = self.doc.addObject("Part::Box", "B1")
        box1.Length, box1.Width, box1.Height = 20.0, 20.0, 10.0
        box2 = self.doc.addObject("Part::Box", "B2")
        box2.Length, box2.Width, box2.Height = 10.0, 10.0, 10.0
        self.doc.recompute()
        joint = joint_group.newObject("App::FeaturePython", "J1")
        Joint(joint, 0)
        joint.Reference1 = (box1, ["Face6"])
        joint.Reference2 = (box2, ["Face5"])
        self.doc.recompute()

        robustify_feature(joint, self.doc, prop_name="Reference1")
        robustify_feature(joint, self.doc, prop_name="Reference2")

        box1.Height = 30.0
        self.doc.recompute()
        _assert_healthy(self, [box1, box2, joint], "after Height change")
        self.assertEqual(joint.State, ["Up-to-date"])
        _ref1_obj, ref1_names = joint.Reference1
        _ref2_obj, ref2_names = joint.Reference2
        self.assertEqual([str(n) for n in ref1_names], ["Face6"])
        self.assertEqual([str(n) for n in ref2_names], ["Face5"])

    def test_07_techdraw_dimension_reference_survives_mutation(self):
        """TechDraw dimension References3D survive a parametric mutation."""
        from fs_bindings import robustify_feature

        box = self.doc.addObject("Part::Box", "Box")
        box.Length, box.Width, box.Height = 20.0, 20.0, 10.0
        self.doc.recompute()
        page = self.doc.addObject("TechDraw::DrawPage", "Page")
        view = self.doc.addObject("TechDraw::DrawViewPart", "View")
        view.Source = [box]
        self.doc.recompute()
        dim = self.doc.addObject("TechDraw::DrawViewDimension", "Dim")
        dim.References3D = [(box, ["Face6"])]

        robustify_feature(dim, self.doc, prop_name="References3D")
        box.Height = 40.0
        self.doc.recompute()
        _assert_healthy(self, [box, dim], "after Height change")
        self.assertEqual([str(n) for _, names in dim.References3D for n in names], ["Face6"])

    def test_08_ambiguous_intent_is_an_honest_warning(self):
        """An unresolvable selector refuses loudly; the native value is untouched."""
        from robust_selector import bind, create, selector
        from fs_selector import Step

        box = self.doc.addObject("Part::Box", "Box")
        box.Length, box.Width, box.Height = 20.0, 20.0, 10.0
        self.doc.recompute()
        sketch = self.doc.addObject("Sketcher::SketchObject", "Sketch")
        sketch.AttachmentSupport = [(box, ["Face6"])]
        sketch.MapMode = "FlatFace"
        self.doc.recompute()

        query = selector(
            "Face",
            [Step("filter", {"name": "planar", "value": True})],
            expected_count=2,
        )
        sel_obj = create(self.doc, box, query, captured_selection=["Face6"])
        record = bind(sel_obj, sketch, "AttachmentSupport")
        self.assertTrue(record.get("proxy"))

        from fs_bindings import update_binding
        ok, message = update_binding(sel_obj, record, query)
        self.assertFalse(ok)
        self.assertIn("unresolved", message)
        support = [(o.Name, tuple(n)) for o, n in sketch.AttachmentSupport]
        self.assertEqual(support, [("Box", ("Face6",))])
        self.assertIn("Warning", sel_obj.HealthStatus)


    def test_09_cross_document_writes_are_refused_explicitly(self):
        """Cross-document sources raise instead of corrupting XLink properties."""
        from fs_bindings import write_linksub
        from fs_selector import Selector, Step

        other = App.newDocument("TestConsumerCompatOtherDoc")
        try:
            box = self.doc.addObject("Part::Box", "Box")
            box.Length, box.Width, box.Height = 20.0, 20.0, 10.0
            foreign = other.addObject("Part::Box", "Foreign")
            foreign.Length, foreign.Width, foreign.Height = 5.0, 5.0, 5.0
            self.doc.recompute()
            other.recompute()
            binder = self.doc.addObject("PartDesign::SubShapeBinder", "Binder")
            query = Selector(
                "Face", [Step("extreme", {"metric": "z", "direction": "max"})],
                expected_count=1,
            )
            refs = query.evaluate(foreign)
            with self.assertRaises(ValueError):
                write_linksub(binder, "Support", refs, foreign)
        finally:
            App.closeDocument(other.Name)

    def test_10_dotted_subelement_paths_are_refused_explicitly(self):
        """Stored assembly paths are never silently rewritten to simple names."""
        from fs_bindings import write_linksub
        from fs_freecad import FeatureRef

        part = self.doc.addObject("App::Part", "Prt")
        body = self.doc.addObject("PartDesign::Body", "Bd")
        part.addObject(body)
        sketch = self.doc.addObject("Sketcher::SketchObject", "S")
        body.addObject(sketch)
        sketch.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(10, 0, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(10, 0, 0), App.Vector(10, 10, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(10, 10, 0), App.Vector(0, 10, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(0, 10, 0), App.Vector(0, 0, 0)))
        pad = self.doc.addObject("PartDesign::Pad", "P")
        body.addObject(pad)
        pad.Profile = sketch
        pad.Length = 10.0
        link = self.doc.addObject("App::Link", "Lnk")
        link.LinkedObject = part
        self.doc.recompute()
        binder = self.doc.addObject("PartDesign::SubShapeBinder", "Binder")
        binder.Support = [(link, ["Bd.P.Face6"])]
        stored = [(o.Name, tuple(n)) for o, n in binder.Support]
        self.assertEqual(stored, [("Lnk", ("Bd.P.Face6",))])
        with self.assertRaises(ValueError):
            write_linksub(binder, "Support", [FeatureRef("Lnk", "Face6", "Face")], link)
        stored = [(o.Name, tuple(n)) for o, n in binder.Support]
        self.assertEqual(stored, [("Lnk", ("Bd.P.Face6",))])


if __name__ == "__main__":
    unittest.main()
