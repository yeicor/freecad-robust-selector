# FreeCAD Robust Selector Workbench

Version 0.6.0

An explicit FreeCAD workbench for robust, feature-based selection. It lets the user turn a normal FreeCAD subelement selection into a persistent semantic selector immediately before the native operation that consumes it, or robustify existing native features across an entire project with 1 click.

## The design rule

The workbench does **not** watch the document, intercept selection, auto-capture native operations, or silently rewrite feature properties.

The user decides when robust selection begins:

### Workflow A: Project-Wide 1-Click "Immunize Document" (Fastest for existing models)
1. Open any existing FreeCAD model with fragile subelement selections (Fillets, Chamfers, Sketches, Binders)
2. Press **Robust Selector → Robustify Entire Document** (`S, A`)
3. Done! Robust Selector scans all features, discovers upstream geometry sources, computes optimal semantic routes, creates persistent selector objects, and rebinds all properties in a single automated pass.

### Workflow B: 1-Click "Robustify Selected Feature" (Single feature)
1. Select an existing native feature in the tree (e.g. `Part::Fillet`, `PartDesign::Fillet`, `Chamfer`, `Sketch`, `SubShapeBinder`)
2. Press **Robust Selector → Robustify Selected Feature** (`S, F`)
3. Done! Robust Selector automatically detects the upstream source object, inspects subelements, plans the optimal semantic route, persists a selector object, and binds the property in under 15ms.

### Workflow C: Explicit Sub-Element Selection (Interactive modeling)
1. Build / recompute the base operation
2. Select the desired Faces / Edges / Vertices / ... in normal FreeCAD
3. Press **Robust Selector → Robust Selection…** (`S, R`)
4. Inspect suggested semantic routes or insert quick engineering presets (e.g. `All Internal Fillet Edges (Concave)`, `All External Chamfer Edges (Convex)`, `Holes by Diameter (M3-M8)`)
5. View candidate geometry metrics (Convexity, Area, Length, Diameter, Center of Mass, Normal direction) and verify live 3D preview highlights
6. Choose the target feature and exact property to drive
7. Press **Create + Bind** (or copy self-contained Python code via **Copy Python Code**)
8. FreeCAD automatically recomputes the selector before its native consumer

Nothing is persisted or bound merely by selecting geometry or by opening the panel.

## Native FreeCAD integration

FreeCAD still receives the ordinary native representation it already understands: an object plus current subelement names such as `Face3` or `Edge7`.

The robust selector is the durable intent. `Face3` / `Edge7` is only the current topology projection:

```text
semantic intent
    |
    v
persistent FeaturePython selector
    |
    | evaluate current source geometry
    v
current native FaceN / EdgeN values
    |
    v
ordinary FreeCAD native property / selection
```

There is no remapping to `Face1`/`Edge1`, no reordering of topology, and no hidden replacement of FreeCAD's selector model.

The target feature remains an ordinary FreeCAD feature. The explicit binding adds a visible `FeatureSelectorSources` LinkList dependency to the target and records the target property in `FeatureSelectorBindings`. Consequently the normal dependency graph can schedule the selector before its consumer without a document observer.

Selector execution occurs in the normal FeaturePython recompute lifecycle. The proxy never calls `recompute()` from `execute()`.

## The editor & diagnostic tools

The **Robust Selection…** command opens the dockable editor panel with full live diagnostics:

1. **Suggested Semantic Routes**:
   Shows multiple breadth-first semantic descriptions. Fewer operations and higher geometric stability are prioritized. Search space is optimized with exact-match pruning, yielding route solutions in < 1 second even on high-complexity parts.

2. **Editable Chain & Quick Presets**:
   - Change selector type or metric;
   - Force design-intent rows;
   - Quick presets combo (Top/Bottom/Side faces, Vertical/Horizontal edges, Concave internal fillets, Convex external chamfers, Smooth tangent edges, Hole diameter ranges, Cylindrical bores, Outer boundaries);
   - Reorder, insert, or remove rows with automatic sub-chain completion.

3. **Candidate & Geometry Metrics Inspector Drawer**:
   Displays an interactive table of all resolved elements showing:
   - Subelement name (`Face1`, `Edge4`)
   - Match status (`✓ Exact`, `⚡ Renumbered`, `Matched`)
   - Geometry classification, dimensions, and convexity badges (e.g. `Plane (100.00 mm²)`, `Line [Concave] (10.00 mm)`, `⌀8.00mm`)
   - 3D Center of Mass coordinates `(X, Y, Z)`
   - Normal or Tangent direction vector `(dX, dY, dZ)`
   - Clicking any candidate immediately isolates and highlights that feature in the 3D viewport.

4. **Live 3D Viewport Highlighting**:
   Provides instant visual feedback in the 3D view as routes or step parameters change.

5. **Self-Contained Python Macro Export**:
   "📋 Copy Python Code" button generates clean, standalone FreeCAD Python scripts reproducing the exact selector queries and bindings without GUI dependencies.

6. **Document-Wide Robustness Audit & 1-Click Immunization**:
   - The **Audit Robust Selections** command scans all selectors in the active document, reporting health status (`OK`, `Warning`, `Error`), resolution timings (ms), and topological renumbering history.
   - The **Robustify Entire Document** command (`S, A`) scans all native features with fragile subelement links and immunizes them all in a single batch.

## Target operation binding

The target is explicit.

Select the native target feature in the Model tree or use **Use selected**. The property list then contains only supported writable native properties. When several properties are present, the user chooses the exact property to own.

No native property is modified until **Create + Bind** or **Bind target property** is pressed.

There is also a **Create Robust Selector** action for saving the semantic selector first and binding later. This is useful when the consumer feature does not exist yet.

For an existing native reference, select the target feature, open the panel, capture the desired source selection, and explicitly bind to the chosen property. **Unbind target property** removes the robust ownership metadata but deliberately leaves the current native value unchanged.

## Supported native property families

### Link / LinkList

Whole-object selectors (`Shape`) can drive ordinary object references.

### LinkSub / LinkSubList (including XLinkSub / XLinkSubList)

The workbench reads and writes the documented object + subelement representation for native subelement properties. A LinkSubList can preserve entries belonging to other source objects. This covers PartDesign dress-ups (`Fillet`/`Chamfer`/`Draft`/`Thickness` `Base`), `Part::Thickness` faces, `Part::Sweep` spines, `SubShapeBinder` supports, TechDraw dimension references, and Assembly joint references (`Reference1`/`Reference2`, verified same-document round-trip).

Same-document XLink writes are enforced: cross-document sources and dotted assembly subelement paths (`Body.Pad.Face6`, which have no verified round-trip form) raise an explicit error instead of being silently corrupted. Placement properties (`AttachmentSupport`) additionally bind through the selector's stable proxy faces, so the consumer shares no direct link with the volatile source at all.

### Legacy Part::PropertyFilletEdges

The adapter handles the classic Part edge-list representation conservatively. Existing per-edge parameter tuples are retained when edge identities remain the same. When identities change, a uniform existing parameter tuple can be transferred. If parameters differ per old edge, the workbench refuses to guess a new mapping.

Single `App::PropertyXLink` (whole-object cross links) has no verified Python assignment semantics and is refused explicitly, as is any genuinely ambiguous intent (a selector that no longer resolves leaves every consumer untouched and reports a visible Warning instead of guessing).

## Selector vocabulary

All of the following are semantic query operations rather than stored topology indices.

### Geometry / topology kinds

`Shape`, `Vertex`, `Edge`, `Wire`, `Face`, `Shell`, `Solid`, `CompSolid`.

### Geometry type filters

Line, circle, ellipse, hyperbola, parabola, B-spline, Bézier, plane, cylinder, cone, sphere, torus, extrusion, revolution, offset, B-spline surface, Bézier surface.

### Common boolean & convexity filters

Planar, curved, linear, circular, elliptical, spherical, cylindrical, conical, toroidal, closed, valid, boundary edge, manifold edge, non-manifold edge, has holes, exactly one wire, multiple wires, isolated vertex, endpoint vertex, and bounding-box contains origin.

Differential edge convexity: `concave` (internal fillet junctions), `convex` (external chamfer seams), or `smooth` (tangent continuous G1/G2 transitions).

### Relative & geometric alignment filters

- `coplanar_with`: Filter faces coplanar with a reference plane or face.
- `coaxial_with`: Filter cylindrical faces or circular edges sharing the central axis of a reference hole or cylinder.

### Orientation filters

Parallel/perpendicular to X/Y/Z, same/opposite direction to X/Y/Z, center on a given axis, faces/edges facing positive or negative X/Y/Z, and angle-to-axis.

### Spatial filters

Positive/negative position along an axis, touching the source bounding-box min/max face, and origin-relative distances.

### Extrema / ordering

Furthest/nearest X, Y, Z; furthest/nearest from origin; closest/furthest from each principal axis; longest/shortest; most/least area; largest/smallest volume; largest/smallest radius; highest/lowest bounding coordinate; largest/smallest bounding box; highest/lowest vertex valence; most/fewest adjacent faces; plus generic metric sorting and taking (the planner uses metric-ranked `sort_take`; new UI chains do not use raw ordinal `take`/`skip`).

### Numeric metrics & ranges

Center X/Y/Z, Euclidean distance, length/perimeter, area, volume, radius, diameter, distance to each principal axis, bounding-box min/max coordinates, bounding-box extents, diagonal, volume, aspect ratio, direction X/Y/Z components, feature counts, hole count, adjacent-face count, and vertex valence.

Metric comparisons support `>`, `>=`, `==`, `<=`, `<`, and `!=` with an explicit tolerance, as well as 1-step interval filtering via `metric_range` (`min <= value <= max`).

Extrema deliberately preserve ties. A tie is not converted into an arbitrary `FaceN` winner.

## Planner guarantees

The planner searches a bounded space so the interactive editor remains predictable on large models.

Its ordering is:

```text
1. fewer selector operations
2. lower stability/readability cost
3. fewer forced steps
```

The planner never accepts a route that drops one of the user's captured elements.

The final selector carries an expected result count. During recompute, an unresolved or ambiguous result does not overwrite the native target property.

## FreeCAD document persistence

A selector is a normal `App::FeaturePython` object grouped under **Robust Selections**. It stores:

- source object;
- feature kind;
- serialized semantic query;
- captured selection audit data;
- expected result count;
- enabled state;
- last resolution state;
- human-readable design intent;
- explicit native-property bindings.

The selector owns semantic intent. The target feature remains the owner of the actual native reference property.

## Scripting API

```python
from robust_selector import selector, resolve, apply, create, bind
from fs_selector import Step

q = selector("Face", [
    Step("filter", {"name": "planar", "value": True}),
    Step("extreme", {"metric": "z", "direction": "max"}),
], expected_count=1, name="top_face")

refs = resolve(App.ActiveDocument.Box, q)
apply(App.ActiveDocument.Box, q)
robust = create(App.ActiveDocument, App.ActiveDocument.Box, q, captured_selection=["Face6"])
bind(robust, App.ActiveDocument.Chamfer, "Base")
```

`Selector.to_json()` and `Selector.from_json()` provide query serialization.

## Installation

Extract the workbench directory into the per-user FreeCAD `Mod` directory returned by `App.getUserAppDataDir()`, restart FreeCAD, and select **Robust Selector** from the workbench selector.

## Validation status

Fully validated under real FreeCAD 1.1.3 (Git 44987) with OCC 7.9.3 and Qt6/PySide.

The test suite covers **66 automated tests** (100% pass rate) across both offline and live FreeCAD environments:
- Real OpenCASCADE geometry evaluation, fast C++ `ancestorsOfType` indexing (58.6x speedup), and differential edge convexity classification (`concave`, `convex`, `smooth`);
- Accelerated route planner with exact-match pruning (28x speedup);
- Real FreeCAD GUI workbench discovery, dock panel interaction, route planning, and step editing;
- Native `Part::Fillet` and `Part::Chamfer` recomputation via `PropertyFilletEdges`;
- Native `PartDesign::Pad`, `Pocket`, `Fillet`, and `SubShapeBinder` (`PropertyXLinkSubList`) workflows;
- 1-Click native feature robustification, candidate metrics inspection, and document health auditing;
- Project-wide 1-click TNP immunization (`FeatureSelector_RobustifyAll`) and standalone Python macro export;
- Quantitative Topological Naming resilience proof of concept (`examples/poc_toponaming_showcase.py`, `examples/poc_complex_flange_assembly.py`, `examples/poc_diagnostics_and_robustify_demo.py`, and `examples/poc_complex_diecast_and_macro_export.py`).

Execute all tests locally via:
```bash
./run_all_tests.py
```

## Compatibility target

Targets the public Python / GUI interfaces documented for the FreeCAD 1.1.x line, with validation anchored to FreeCAD 1.1.4. It avoids private C++ APIs and third-party runtime dependencies.

Official references used while implementing the workbench:

- https://github.com/FreeCAD/FreeCAD/releases/tag/1.1.4
- https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/FeaturePython_methods.md
- https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/FeaturePython_Custom_Properties.md
- https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Code_snippets.md
