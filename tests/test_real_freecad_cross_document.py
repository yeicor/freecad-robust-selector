"""Real FreeCAD test suite: cross-file references through App::Link.

Same-document link objects that point at external part files are first-class
sources: robustifying through them works, and after the external file is
edited, saved, closed, and reopened (the reload), a normal recompute updates
the selectors and their consumers with no false errors.  Genuinely
cross-document sources (no link involved) are refused with an explicit error
instead of building a selector that could never update.
"""
import os
import tempfile
import unittest

try:
    import FreeCAD as App
    import Part
except ImportError:
    App = None


def setUpModule():
    if App is None:
        raise unittest.SkipTest("FreeCAD not available in Python environment")


def _close_doc(name):
    try:
        App.closeDocument(name)
    except Exception:
        pass


class TestCrossDocumentReferences(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.part_path = os.path.join(self._tmp.name, "ExtPart.FCStd")
        self.asm_path = os.path.join(self._tmp.name, "AsmFile.FCStd")

        part = App.newDocument("XPartDoc")
        stock = part.addObject("Part::Box", "Stock")
        stock.Length, stock.Width, stock.Height = 20.0, 20.0, 10.0
        part.recompute()
        part.saveAs(self.part_path)

        asm = App.newDocument("XAsmDoc")
        asm.saveAs(self.asm_path)
        self.part_doc = part
        self.asm_doc = asm

    def tearDown(self):
        _close_doc("XAsmDoc")
        _close_doc("XPartDoc")
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def _make_link(self):
        link = self.asm_doc.addObject("App::Link", "StockLink")
        link.LinkedObject = self.part_doc.getObject("Stock")
        self.asm_doc.recompute()
        return link

    def test_01_joint_through_link_survives_external_reload(self):
        """Joint faces on a linked part update across the external reload cycle."""
        import sys
        sys.path.insert(0, "/usr/lib/freecad/Mod/Assembly")
        import UtilsAssembly
        from JointObject import Joint
        from fs_bindings import robustify_feature

        asm = self.asm_doc.addObject("Assembly::AssemblyObject", "Asm")
        joint_group = UtilsAssembly.getJointGroup(asm)
        link = self._make_link()
        local = self.asm_doc.addObject("Part::Box", "Local")
        local.Length, local.Width, local.Height = 10.0, 10.0, 10.0
        self.asm_doc.recompute()
        joint = joint_group.newObject("App::FeaturePython", "J1")
        Joint(joint, 0)
        joint.Reference1 = (link, ["Face6"])
        joint.Reference2 = (local, ["Face5"])
        self.asm_doc.recompute()

        selector, _plan = robustify_feature(joint, self.asm_doc)
        self.assertEqual(selector.BaseObject.Name, "StockLink")

        # External edit: change, save, close (as if edited in another session).
        self.part_doc.getObject("Stock").Height = 30.0
        self.part_doc.recompute()
        self.part_doc.save()
        App.closeDocument(self.part_doc.Name)
        # Reload the external reference, then recompute normally.
        self.part_doc = App.openDocument(self.part_path)
        self.asm_doc.recompute()

        self.assertEqual(link.Shape.BoundBox.ZMax, 30.0)
        ref_obj, ref_names = joint.Reference1
        self.assertEqual(ref_obj.Name, "StockLink")
        self.assertEqual([str(n) for n in ref_names], ["Face6"])
        for obj in (link, joint, selector, self.asm_doc.getObject("Asm")):
            self.assertNotIn("Invalid", obj.State, f"{obj.Name}: {obj.State}")
            self.assertNotIn("Error", obj.State, f"{obj.Name}: {obj.State}")
        self.assertIn("StockLink", selector.Resolved)

    def test_02_sketch_proxy_through_link_survives_external_reload(self):
        """Proxy-attached sketches follow linked parts across the reload cycle."""
        from fs_bindings import robustify_feature

        link = self._make_link()
        sketch = self.asm_doc.addObject("Sketcher::SketchObject", "Sketch")
        sketch.AttachmentSupport = [(link, ["Face6"])]
        sketch.MapMode = "FlatFace"
        sketch.addGeometry(Part.LineSegment(App.Vector(-2, -2, 0), App.Vector(2, -2, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(2, -2, 0), App.Vector(2, 2, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(2, 2, 0), App.Vector(-2, 2, 0)))
        sketch.addGeometry(Part.LineSegment(App.Vector(-2, 2, 0), App.Vector(-2, -2, 0)))
        self.asm_doc.recompute()
        self.assertAlmostEqual(sketch.Placement.Base.z, 10.0, places=3)

        selector, _plan = robustify_feature(sketch, self.asm_doc)
        support = [(o.Name, tuple(n)) for o, n in sketch.AttachmentSupport]
        self.assertEqual(support, [(selector.Name, ("Face1",))])

        self.part_doc.getObject("Stock").Height = 30.0
        self.part_doc.recompute()
        self.part_doc.save()
        App.closeDocument(self.part_doc.Name)
        self.part_doc = App.openDocument(self.part_path)
        self.asm_doc.recompute()

        for obj in (link, sketch, selector):
            self.assertEqual(obj.State, ["Up-to-date"], f"{obj.Name}: {obj.State}")
        self.assertAlmostEqual(sketch.Placement.Base.z, 30.0, places=3)

    def test_03_direct_cross_document_source_is_refused(self):
        """A source in another document raises instead of building debris."""
        from robust_selector import selector as make_selector
        from fs_bindings import prepare_robustify_plan
        from fs_document import create_selector_object
        from fs_selector import Step

        foreign = self.part_doc.getObject("Stock")
        target = self.asm_doc.addObject("Part::Box", "Target")
        query = make_selector("Face", [Step("extreme", {"metric": "z", "direction": "max"})], expected_count=1)

        with self.assertRaises(ValueError):
            create_selector_object(self.asm_doc, foreign, query)
        with self.assertRaises(ValueError):
            prepare_robustify_plan(self.asm_doc, target, foreign, "AttachmentSupport", "LinkSub", ["Face6"], "Face")


if __name__ == "__main__":
    unittest.main()
