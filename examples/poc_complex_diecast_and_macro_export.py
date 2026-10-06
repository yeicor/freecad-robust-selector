#!/usr/bin/env python3
"""Complex Geometry & Macro Generation Showcase.

Demonstrates:
1. High-speed handling of complex multi-feature CAD geometry (housing with bosses & pockets).
2. Edge convexity classification: 1-step semantic selection of internal fillet edges (concave)
   vs external chamfer edges (convex).
3. Metric range & diameter filtering for bolt clearance patterns.
4. Project-wide 1-click TNP immunization: `robustify_all_features()`.
5. Standalone FreeCAD Python macro generation via `selector_to_python()`.
"""
import os
import sys
import time

FREECAD_LIB = "/usr/lib/freecad/lib"
if os.path.isdir(FREECAD_LIB) and FREECAD_LIB not in sys.path:
    sys.path.insert(0, FREECAD_LIB)

import FreeCAD as App
import Part

from fs_bindings import (
    audit_document_selectors,
    format_audit_report,
    format_robustify_all_report,
    robustify_all_features,
)
from fs_selector import Selector, Step, selector_to_python


def run_showcase():
    print("=" * 78)
    print("  FREECAD FEATURE SELECTOR: COMPLEX GEOMETRY & MACRO GENERATION")
    print("=" * 78)

    doc = App.newDocument("DiecastComplexShowcase")

    print("\n[Step 1] Constructing complex die-cast casing...")
    # Base casing block
    base = Part.makeBox(120, 80, 30)

    # Hollow internal cavity pocket
    pocket = Part.makeBox(104, 64, 25, App.Vector(8, 8, 8))
    casing = base.cut(pocket)

    # Add 4 internal corner reinforcement bosses
    bosses = []
    for bx, by in ((16, 16), (104, 16), (16, 64), (104, 64)):
        boss = Part.makeCylinder(7, 22, App.Vector(bx, by, 8))
        bosses.append(boss)
        casing = casing.fuse(boss)

    # Cut M4 tapped holes (diameter 4.0mm) into the bosses
    for bx, by in ((16, 16), (104, 16), (16, 64), (104, 64)):
        hole = Part.makeCylinder(2.0, 30, App.Vector(bx, by, 5))
        casing = casing.cut(hole)

    # Cut 2 large cable gland pass-throughs (diameter 16.0mm)
    gland1 = Part.makeCylinder(8.0, 40, App.Vector(60, -5, 18), App.Vector(0, 1, 0))
    gland2 = Part.makeCylinder(8.0, 40, App.Vector(60, 45, 18), App.Vector(0, 1, 0))
    casing = casing.cut(gland1).cut(gland2)

    housing = doc.addObject("Part::Feature", "CasingBody")
    housing.Shape = casing
    doc.recompute()

    faces_count = len(housing.Shape.Faces)
    edges_count = len(housing.Shape.Edges)
    print(f"  Constructed casing: {faces_count} faces, {edges_count} edges, {len(housing.Shape.Vertexes)} vertices.")

    print("\n[Step 2] Semantic Edge Convexity: Selecting all internal concave fillet edges...")
    t0 = time.perf_counter()
    sel_concave = Selector(
        kind="Edge",
        steps=(
            Step("filter", {"name": "convexity", "value": "concave"}),
            Step("filter", {"name": "linear", "value": True}),
        ),
    )
    concave_edges = sel_concave.evaluate(housing)
    eval_ms = (time.perf_counter() - t0) * 1000.0
    print(f"  Matched {len(concave_edges)} concave junction edges in {eval_ms:.2f} ms!")
    print(f"  Sample subelements: {[r.subname for r in concave_edges[:6]]}...")

    print("\n[Step 3] Semantic Diameter Range: Isolating M4 screw holes (⌀ 3.5mm – 4.5mm)...")
    sel_m4 = Selector(
        kind="Face",
        steps=(
            Step("filter", {"name": "cylindrical", "value": True}),
            Step("filter", {"name": "metric_range", "value": {"metric": "diameter", "min": 3.5, "max": 4.5}}),
        ),
    )
    m4_faces = sel_m4.evaluate(housing)
    print(f"  Matched {len(m4_faces)} screw hole faces: {[r.subname for r in m4_faces]}")

    print("\n[Step 4] Creating native features referencing fragile topology...")
    # Native fillet on 2 exterior corner edges
    fillet = doc.addObject("Part::Fillet", "CornerFillet")
    fillet.Base = housing
    fillet.Edges = [(1, 1.5, 1.5), (2, 1.5, 1.5)]
    doc.recompute()

    # SubShapeBinder referencing top mounting face
    binder = doc.addObject("PartDesign::SubShapeBinder", "LidMountBinder")
    binder.Support = [(housing, ["Face1"])]
    doc.recompute()
    print("  Native Part::Fillet and SubShapeBinder created using fragile element references.")

    print("\n[Step 5] 1-Click Project-Wide TNP Immunization ('Robustify All Features')...")
    summary = robustify_all_features(doc)
    print("\n" + format_robustify_all_report(summary))

    print("\n[Step 6] Standalone Macro Export: Generating reproducible Python code for engineers...")
    macro_code = selector_to_python(sel_concave, "CasingBody")
    print("-" * 60)
    print(macro_code)
    print("-" * 60)

    print("\n[Step 7] Final Document Audit:")
    audit = audit_document_selectors(doc)
    print("\n" + format_audit_report(audit))

    print("=" * 78)
    print("  COMPLEX GEOMETRY SHOWCASE COMPLETE: Fast, Robust, Production-Ready!")
    print("=" * 78)

    App.closeDocument(doc.Name)


if __name__ == "__main__":
    run_showcase()
