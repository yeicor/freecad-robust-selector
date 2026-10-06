"""FreeCAD-specific geometry helpers.

The core selector stores semantic operations.  This module is the only layer
that turns the current FreeCAD shape into feature measurements and, finally,
into the native ``Object, SubElement`` pair FreeCAD commands consume.

The implementation targets the public Python API shipped with FreeCAD 1.1.4.
It deliberately does not depend on private Qt/C++ bindings or third-party
packages.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

try:  # pragma: no cover - exercised inside FreeCAD
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
except ModuleNotFoundError:  # pragma: no cover - offline unit tests
    App = None
    Gui = None
    Part = None


FEATURE_KINDS = ("Shape", "Vertex", "Edge", "Wire", "Face", "Shell", "Solid", "CompSolid")


def doc_name(obj: Any) -> str:
    return str(getattr(getattr(obj, "Document", None), "Name", "") or "")


def feature_name(obj: Any) -> str:
    return str(getattr(obj, "Name", "") or "")


def has_shape(obj: Any) -> bool:
    shape = getattr(obj, "Shape", None)
    try:
        return shape is not None and not shape.isNull()
    except (AttributeError, TypeError):
        return shape is not None


def _plural(kind: str) -> str:
    return {
        "Vertex": "Vertexes",
        "Edge": "Edges",
        "Wire": "Wires",
        "Face": "Faces",
        "Shell": "Shells",
        "Solid": "Solids",
        "CompSolid": "CompSolids",
    }.get(kind, "")


def features_from_object(obj: Any, kind: str) -> list[Any]:
    """Return current sub-elements in FreeCAD/OCC's native order.

    That order is *only* used to enumerate current topology.  No selector
    semantics are based on the returned ordinal.
    """
    if not has_shape(obj):
        return []
    if kind == "Shape":
        return [obj.Shape]
    attr = _plural(kind)
    return list(getattr(obj.Shape, attr, [])) if attr else []


def shape_type_from_subname(subname: str) -> Optional[str]:
    text = str(subname or "")
    # Assembly subelement paths terminate in the element name ("Body.Pad.Face6").
    leaf = text.rsplit(".", 1)[-1]
    for kind in FEATURE_KINDS:
        if kind != "Shape" and leaf.startswith(kind) and leaf[len(kind):].isdigit():
            return kind
    return None


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def vector_tuple(v: Any) -> Tuple[float, float, float]:
    return float(v.x), float(v.y), float(v.z)


def _zero_vector() -> Any:
    if App is None:
        return _TupleVector(0.0, 0.0, 0.0)
    return App.Vector(0.0, 0.0, 0.0)


@dataclass(frozen=True)
class _TupleVector:
    x: float
    y: float
    z: float

    def sub(self, other: Any) -> "_TupleVector":
        return _TupleVector(self.x - other.x, self.y - other.y, self.z - other.z)

    def dot(self, other: Any) -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z

    def Length(self) -> float:  # noqa: N802 - FreeCAD-like shim for tests
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)



def transform_point(obj: Any, p: Any) -> Any:
    placement = getattr(obj, "Placement", None)
    if placement is None:
        return p
    try:
        return placement.multVec(p)
    except Exception:
        return p


def transform_vector(obj: Any, v: Any) -> Any:
    placement = getattr(obj, "Placement", None)
    if placement is None:
        return v
    try:
        return placement.Rotation.multVec(v)
    except Exception:
        return v


def feature_center(obj: Any, shape: Any) -> Any:
    try:
        return transform_point(obj, shape.CenterOfMass)
    except Exception:
        try:
            return transform_point(obj, shape.BoundBox.Center)
        except Exception:
            return _zero_vector()


def feature_area(shape: Any) -> float:
    return safe_float(getattr(shape, "Area", 0.0))


def feature_length(shape: Any) -> float:
    return safe_float(getattr(shape, "Length", 0.0))


def feature_perimeter(shape: Any) -> float:
    """Return a boundary length for faces/wires, falling back to TopoShape.Length."""
    try:
        wires = list(getattr(shape, "Wires", []))
        if wires:
            return sum(feature_length(edge) for wire in wires for edge in getattr(wire, "Edges", []))
    except Exception:
        pass
    return feature_length(shape)


def feature_volume(shape: Any) -> float:
    return safe_float(getattr(shape, "Volume", 0.0))


def feature_radius(shape: Any) -> Optional[float]:
    for owner in (getattr(shape, "Curve", None), getattr(shape, "Surface", None)):
        if owner is None:
            continue
        value = getattr(owner, "Radius", None)
        if value is not None:
            return safe_float(value)
    return None


def geometry_type(shape: Any) -> str:
    """Return normalized OCC curve/surface type for the current subshape."""
    geo = getattr(shape, "Curve", None)
    if geo is None:
        geo = getattr(shape, "Surface", None)
    if geo is None:
        return type(shape).__name__.upper()

    name = type(geo).__name__
    aliases = {
        "Line": "LINE",
        "LineSegment": "LINE",
        "Circle": "CIRCLE",
        "ArcOfCircle": "CIRCLE",
        "Ellipse": "ELLIPSE",
        "ArcOfEllipse": "ELLIPSE",
        "Hyperbola": "HYPERBOLA",
        "ArcOfHyperbola": "HYPERBOLA",
        "Parabola": "PARABOLA",
        "ArcOfParabola": "PARABOLA",
        "BSplineCurve": "BSPLINE",
        "BezierCurve": "BEZIER",
        "Plane": "PLANE",
        "Cylinder": "CYLINDER",
        "Cone": "CONE",
        "Sphere": "SPHERE",
        "Toroid": "TORUS",
        "Torus": "TORUS",
        "SurfaceOfExtrusion": "EXTRUSION",
        "SurfaceOfRevolution": "REVOLUTION",
        "OffsetSurface": "OFFSET",
        "BSplineSurface": "BSPLINE_SURFACE",
        "BezierSurface": "BEZIER_SURFACE",
    }
    return aliases.get(name, name.upper())


def representative_direction(obj: Any, shape: Any, kind: str) -> Optional[Any]:
    """Return a useful unit direction/normal in global coordinates."""
    if kind == "Face":
        try:
            center_local = shape.CenterOfMass
            surface = shape.Surface
            u, v = surface.parameter(center_local)
            n = transform_vector(obj, shape.normalAt(u, v))
            try:
                return n.normalized()
            except Exception:
                n.normalize()
                return n
        except Exception:
            return None
    if kind == "Edge":
        try:
            verts = list(getattr(shape, "Vertexes", []))
            if len(verts) >= 2:
                d = transform_vector(obj, verts[-1].Point.sub(verts[0].Point))
                try:
                    return d.normalized()
                except Exception:
                    d.normalize()
                    return d
        except Exception:
            pass
        try:
            curve = shape.Curve
            if hasattr(curve, "getParameterBounds"):
                first, last = curve.getParameterBounds()
            else:
                first, last = (0.0, 1.0)
            d = transform_vector(obj, shape.tangentAt((first + last) / 2.0))
            try:
                return d.normalized()
            except Exception:
                d.normalize()
                return d
        except Exception:
            return None
    return None


def bbox_tuple(obj: Any, shape: Any) -> tuple[float, float, float, float, float, float]:
    """Return global-axis-aligned bounding limits."""
    try:
        bb = shape.BoundBox
        corners = []
        for x in (bb.XMin, bb.XMax):
            for y in (bb.YMin, bb.YMax):
                for z in (bb.ZMin, bb.ZMax):
                    if App is None:
                        corners.append(_TupleVector(x, y, z))
                    else:
                        corners.append(App.Vector(x, y, z))
        global_corners = [transform_point(obj, p) for p in corners]
        xs = [p.x for p in global_corners]
        ys = [p.y for p in global_corners]
        zs = [p.z for p in global_corners]
        return min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)
    except Exception:
        c = feature_center(obj, shape)
        return c.x, c.y, c.z, c.x, c.y, c.z


def is_closed(shape: Any) -> Optional[bool]:
    try:
        return bool(shape.isClosed())
    except Exception:
        return None


def is_valid(shape: Any) -> Optional[bool]:
    try:
        return bool(shape.isValid())
    except Exception:
        return None


def face_hole_count(shape: Any) -> int:
    try:
        wires = list(shape.Wires)
        if not wires:
            return 0
        return max(0, len(wires) - 1)
    except Exception:
        return 0


def adjacent_face_counts(source_obj: Any) -> dict[str, int]:
    """Count source faces incident to each edge, using fast OCC ancestors or identity fallback."""
    counts: dict[str, int] = {}
    edges = features_from_object(source_obj, "Edge")
    shape = getattr(source_obj, "Shape", None)
    if shape is not None and hasattr(shape, "ancestorsOfType") and Part is not None:
        try:
            for idx, edge in enumerate(edges, 1):
                try:
                    counts[f"Edge{idx}"] = len(shape.ancestorsOfType(edge, Part.Face))
                except Exception:
                    counts[f"Edge{idx}"] = 0
            return counts
        except Exception:
            pass
    faces = features_from_object(source_obj, "Face")
    for idx, edge in enumerate(edges, 1):
        count = 0
        for face in faces:
            try:
                if any(edge.isSame(candidate) for candidate in face.Edges):
                    count += 1
            except Exception:
                continue
        counts[f"Edge{idx}"] = count
    return counts


def adjacent_vertex_counts(source_obj: Any) -> dict[str, int]:
    """Count incident edges for each source vertex using fast OCC ancestors or identity fallback."""
    counts: dict[str, int] = {}
    vertices = features_from_object(source_obj, "Vertex")
    shape = getattr(source_obj, "Shape", None)
    if shape is not None and hasattr(shape, "ancestorsOfType") and Part is not None:
        try:
            for idx, vertex in enumerate(vertices, 1):
                try:
                    counts[f"Vertex{idx}"] = len(shape.ancestorsOfType(vertex, Part.Edge))
                except Exception:
                    counts[f"Vertex{idx}"] = 0
            return counts
        except Exception:
            pass
    edges = features_from_object(source_obj, "Edge")
    for idx, vertex in enumerate(vertices, 1):
        count = 0
        for edge in edges:
            try:
                if any(vertex.isSame(candidate) for candidate in edge.Vertexes):
                    count += 1
            except Exception:
                continue
        counts[f"Vertex{idx}"] = count
    return counts


def edge_convexities(source_obj: Any) -> dict[str, str]:
    """Classify manifold edges into 'convex' (exterior/chamfer), 'concave' (interior/fillet), or 'smooth'.

    Non-manifold or open sheet edges are classified as 'open' or 'non_manifold'.
    """
    counts: dict[str, str] = {}
    shape = getattr(source_obj, "Shape", None)
    if shape is None or not hasattr(shape, "ancestorsOfType") or Part is None:
        return counts

    edges = features_from_object(source_obj, "Edge")
    for idx, edge in enumerate(edges, 1):
        subname = f"Edge{idx}"
        try:
            faces = shape.ancestorsOfType(edge, Part.Face)
            if len(faces) == 1:
                counts[subname] = "open"
                continue
            if len(faces) != 2:
                counts[subname] = "non_manifold"
                continue

            f1, f2 = faces[0], faces[1]
            first_param = getattr(edge, "FirstParameter", 0.0)
            last_param = getattr(edge, "LastParameter", 1.0)
            mid = (first_param + last_param) / 2.0
            p_mid = edge.valueAt(mid)
            t = edge.tangentAt(mid)

            u1, v1 = f1.Surface.parameter(p_mid)
            n1 = f1.normalAt(u1, v1)
            u2, v2 = f2.Surface.parameter(p_mid)
            n2 = f2.normalAt(u2, v2)

            orient = "Forward"
            for fe in getattr(f1, "Edges", []):
                if fe.isSame(edge):
                    orient = getattr(fe, "Orientation", "Forward")
                    break

            t_bound = t if orient == "Forward" else t.multiply(-1.0)
            n_out = t_bound.cross(n1)
            dot_conv = n_out.dot(n2)
            dot_n = max(-1.0, min(1.0, n1.dot(n2)))

            if dot_n > 0.9999:
                counts[subname] = "smooth"
            elif dot_conv > 1e-4:
                counts[subname] = "convex"
            elif dot_conv < -1e-4:
                counts[subname] = "concave"
            else:
                counts[subname] = "smooth"
        except Exception:
            counts[subname] = "unknown"
    return counts


def feature_axis_info(obj: Any, shape: Any) -> tuple[Optional[Any], Optional[Any]]:
    """Extract axis direction and origin point in global coordinates, if applicable."""
    surf = getattr(shape, "Surface", None)
    curve = getattr(shape, "Curve", None)
    target = surf if surf is not None else curve
    if target is None:
        return None, None

    axis = getattr(target, "Axis", None)
    center = getattr(target, "Center", None) or getattr(target, "Position", None)
    if axis is None or center is None:
        return None, None

    try:
        global_dir = transform_vector(obj, axis)
        try:
            global_dir = global_dir.normalized()
        except Exception:
            pass
        global_center = transform_point(obj, center)
        return global_dir, global_center
    except Exception:
        return None, None


@dataclass(frozen=True)
class FeatureRef:
    object_name: str
    subname: str
    kind: str

    @property
    def selection_tuple(self) -> tuple[str, str]:
        return self.object_name, self.subname


def make_ref(obj: Any, subname: str, kind: Optional[str] = None) -> FeatureRef:
    resolved_kind = kind or shape_type_from_subname(subname) or "Shape"
    return FeatureRef(feature_name(obj), subname, resolved_kind)


def resolve_ref(obj: Any, ref: FeatureRef) -> Any:
    if ref.kind == "Shape" or not ref.subname:
        return getattr(obj, "Shape", None)
    shape = getattr(obj, "Shape", None)
    if shape is not None and hasattr(shape, "getElement"):
        try:
            sub = shape.getElement(ref.subname)
            if sub is not None:
                return sub
        except Exception:
            pass
    if hasattr(obj, "getSubObject"):
        try:
            sub = obj.getSubObject(ref.subname)
            if sub is not None:
                return sub
        except Exception:
            pass
    attr = _plural(ref.kind)
    if shape is not None and hasattr(shape, attr):
        try:
            elements = list(getattr(shape, attr, []))
            m = re.search(r"\d+$", ref.subname)
            if m:
                idx = int(m.group(0)) - 1
                if 0 <= idx < len(elements):
                    return elements[idx]
        except Exception:
            pass
    if shape is not None and hasattr(shape, "getSubShape"):
        return shape.getSubShape(ref.subname)
    return None


def selection_pairs(refs: Iterable[FeatureRef]) -> list[tuple[str, str]]:
    return [(r.object_name, r.subname) for r in refs]


def add_selection(refs: Iterable[FeatureRef], clear: bool = True) -> list[FeatureRef]:
    """Apply a resolved selector through FreeCAD's documented GUI selection API."""
    if Gui is None or App is None:
        return []
    refs = list(refs)
    if clear:
        Gui.Selection.clearSelection()
    doc = App.ActiveDocument
    if doc is None:
        return []
    applied: list[FeatureRef] = []
    for ref in refs:
        obj = doc.getObject(ref.object_name)
        if obj is None:
            continue
        try:
            result = Gui.Selection.addSelection(obj) if ref.kind == "Shape" else Gui.Selection.addSelection(obj, ref.subname)
            if result is not False:
                applied.append(ref)
        except Exception:
            continue
    return applied
