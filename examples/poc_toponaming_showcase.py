#!/usr/bin/env python3
"""Proof of Concept: Topological Naming Problem (TNP) Resilience in FreeCAD.

This script demonstrates side-by-side:
1. Native FreeCAD workflow with brittle subelement indexing (FaceN / EdgeN).
2. FeatureSelector robust semantic selection workflow.

It simulates a real-world engineering project where upstream design changes
cause OpenCASCADE to renumber all topological subelements.
"""
from __future__ import annotations

import time
import FreeCAD as App
import Part
import Sketcher

from robust_selector import bind, create, selector
from fs_selector import Step


def run_poc():
    print("=" * 76)
    print("  FREECAD FEATURE SELECTOR: ROBUST SELECTION PROOF OF CONCEPT")
    print("  Testing Topological Naming Problem Resilience on Real Geometry")
    print("=" * 76)

    # -------------------------------------------------------------------------
    # STAGE 1: Base Geometry Setup
    # -------------------------------------------------------------------------
    doc_native = App.newDocument("NativeBrittleModel")
    doc_robust = App.newDocument("RobustSemanticModel")

    print("\n[Step 1] Creating base enclosure geometry in both models...")
    for doc in (doc_native, doc_robust):
        box = doc.addObject("Part::Box", "BaseEnclosure")
        box.Length = 120.0
        box.Width = 80.0
        box.Height = 50.0
        doc.recompute()

    box_native = doc_native.BaseEnclosure
    box_robust = doc_robust.BaseEnclosure

    print(f"  Base Box dimensions: 120 x 80 x 50 mm")
    print(f"  Initial faces count: {len(box_native.Shape.Faces)}")

    # Identify initial top face
    top_face_initial_name = None
    for i, f in enumerate(box_native.Shape.Faces, 1):
        if abs(f.CenterOfMass.z - 50.0) < 1e-4:
            top_face_initial_name = f"Face{i}"
            break
    print(f"  Initial top face identifier: {top_face_initial_name} (Z = 50.0 mm)")

    # -------------------------------------------------------------------------
    # STAGE 2: Downstream Feature Attachment
    # -------------------------------------------------------------------------
    print("\n[Step 2] Attaching downstream feature (Sensor Mount Sketch) to the top face...")

    # A) Native FreeCAD: directly attach to initial face name (Face6)
    sketch_native = doc_native.addObject("Sketcher::SketchObject", "SensorMountSketch")
    sketch_native.AttachmentSupport = [(box_native, [top_face_initial_name])]
    sketch_native.MapMode = "FlatFace"
    doc_native.recompute()

    print(f"  [Native Model] Sketch attached directly to: {box_native.Name}.{top_face_initial_name}")
    print(f"  [Native Model] Initial sketch placement Z: {sketch_native.Placement.Base.z:.1f} mm")

    # B) FeatureSelector: bind to semantic query (Planar face, Normal +Z, Max Z)
    q_top = selector("Face", [
        Step("filter", {"name": "planar", "value": True}),
        Step("filter", {"name": "axis_same_direction", "value": "Z"}),
        Step("extreme", {"metric": "z", "direction": "max"}),
    ], expected_count=1, name="top_mounting_face")

    robust_selector = create(doc_robust, box_robust, q_top, captured_selection=[top_face_initial_name])
    sketch_robust = doc_robust.addObject("Sketcher::SketchObject", "SensorMountSketch")
    sketch_robust.MapMode = "FlatFace"
    bind(robust_selector, sketch_robust, "AttachmentSupport")
    doc_robust.recompute()

    print(f"  [Robust Model] Robust selector created: {robust_selector.Label}")
    print(f"  [Robust Model] Initial resolved face: {robust_selector.Resolved}")
    print(f"  [Robust Model] Initial sketch placement Z: {sketch_robust.Placement.Base.z:.1f} mm")

    # -------------------------------------------------------------------------
    # STAGE 3: The Topological Naming Mutation
    # -------------------------------------------------------------------------
    print("\n[Step 3] Introducing upstream design change (adding a 45-degree chamfer to edge 1)...")
    print("  This causes OpenCASCADE to renumber all topological entities!")

    # In Native Model: apply chamfer to Edge1
    chamfer_native = doc_native.addObject("Part::Chamfer", "CableReliefChamfer")
    chamfer_native.Base = box_native
    chamfer_native.Edges = [(1, 15.0, 15.0)]
    doc_native.recompute()

    # Re-link native sketch to chamfer object maintaining the same subname 'Face6'
    sketch_native.AttachmentSupport = [(chamfer_native, [top_face_initial_name])]
    doc_native.recompute()

    # In Robust Model: apply same chamfer to BaseEnclosure
    chamfer_robust = doc_robust.addObject("Part::Chamfer", "CableReliefChamfer")
    chamfer_robust.Base = box_robust
    chamfer_robust.Edges = [(1, 15.0, 15.0)]
    doc_robust.recompute()

    # Update selector's base object to the new upstream feature
    robust_selector.BaseObject = chamfer_robust
    start_t = time.perf_counter_ns()
    doc_robust.recompute()
    elapsed_us = (time.perf_counter_ns() - start_t) / 1000.0

    # -------------------------------------------------------------------------
    # STAGE 4: Evaluation and Comparison
    # -------------------------------------------------------------------------
    print("\n[Step 4] Evaluating model integrity after topological renumbering:")

    # Check Native Model
    native_z = sketch_native.Placement.Base.z
    native_normal = sketch_native.Placement.Rotation.multVec(App.Vector(0, 0, 1))
    print(f"\n  === NATIVE FREECAD RESULT ===")
    print(f"  Attachment subelement: {top_face_initial_name}")
    print(f"  Sketch placement Z:    {native_z:.1f} mm (Expected: 50.0 mm)")
    print(f"  Sketch normal vector:  ({native_normal.x:.2f}, {native_normal.y:.2f}, {native_normal.z:.2f}) (Expected: (0, 0, 1))")
    native_failed = (abs(native_z - 50.0) > 1.0) or (abs(native_normal.z - 1.0) > 0.01)
    if native_failed:
        print("  >>> STATUS: BROKEN! The sketch jumped to a vertical side face due to TNP! <<<")
    else:
        print("  >>> STATUS: Intact.")

    # Check Robust Model
    robust_z = sketch_robust.Placement.Base.z
    robust_normal = sketch_robust.Placement.Rotation.multVec(App.Vector(0, 0, 1))
    print(f"\n  === FEATURE SELECTOR RESULT ===")
    print(f"  Current resolved face: {robust_selector.Resolved}")
    print(f"  Sketch placement Z:    {robust_z:.1f} mm (Expected: 50.0 mm)")
    print(f"  Sketch normal vector:  ({robust_normal.x:.2f}, {robust_normal.y:.2f}, {robust_normal.z:.2f}) (Expected: (0, 0, 1))")
    print(f"  Recompute overhead:    {elapsed_us:.1f} µs ({elapsed_us / 1000.0:.3f} ms)")
    robust_success = (abs(robust_z - 50.0) < 1e-3) and (abs(robust_normal.z - 1.0) < 1e-3)
    if robust_success:
        print("  >>> STATUS: PERFECT SUCCESS! Semantic intent preserved despite face renumbering! <<<")
    else:
        print("  >>> STATUS: Failed.")

    # -------------------------------------------------------------------------
    # STAGE 5: Summary Report
    # -------------------------------------------------------------------------
    print("\n" + "=" * 76)
    print("  PROOF OF CONCEPT SUMMARY")
    print("=" * 76)
    print(f"  Native FreeCAD (Index-based):  {'FAILED (Subelement shifted)' if native_failed else 'PASSED'}")
    print(f"  Feature Selector (Semantic):   {'PASSED (100% Geometry Preserved)' if robust_success else 'FAILED'}")
    print(f"  Evaluation Time:               {elapsed_us / 1000.0:.3f} ms")
    print("=" * 76 + "\n")

    App.closeDocument(doc_native.Name)
    App.closeDocument(doc_robust.Name)
    return robust_success and native_failed


if __name__ == "__main__":
    success = run_poc()
    import sys
    sys.exit(0 if success else 1)
