"""Semantic, feature-based selection for FreeCAD.

Selectors describe geometric/topological intent.  Native names such as ``Face7``
are generated only when the query is evaluated against the *current* shape and
are handed to FreeCAD's normal selection/property APIs.  They are never stored as
the selector's design intent.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from fs_freecad import (
    FEATURE_KINDS,
    FeatureRef,
    adjacent_face_counts,
    adjacent_vertex_counts,
    bbox_tuple,
    edge_convexities,
    feature_area,
    feature_axis_info,
    feature_center,
    feature_length,
    feature_perimeter,
    feature_radius,
    feature_volume,
    features_from_object,
    face_hole_count,
    geometry_type,
    is_closed,
    is_valid,
    make_ref,
    representative_direction,
    resolve_ref,
)


EPS = 1e-7
AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}
AXIS_NAMES = tuple(AXES)
METRICS = (
    "x", "y", "z", "distance", "length", "perimeter", "area", "volume", "radius", "diameter", "compactness",
    "axis_distance_x", "axis_distance_y", "axis_distance_z",
    "bbox_min_x", "bbox_max_x", "bbox_min_y", "bbox_max_y", "bbox_min_z", "bbox_max_z",
    "bbox_size_x", "bbox_size_y", "bbox_size_z", "bbox_diagonal", "bbox_volume", "bbox_aspect_ratio",
    "direction_x", "direction_y", "direction_z", "normal_x", "normal_y", "normal_z",
    "vertex_count", "edge_count", "wire_count", "face_count", "shell_count", "solid_count",
    "hole_count", "adjacent_face_count", "vertex_valence",
)
GEOMETRY_TYPES = (
    "LINE", "CIRCLE", "ELLIPSE", "HYPERBOLA", "PARABOLA", "BSPLINE", "BEZIER",
    "PLANE", "CYLINDER", "CONE", "SPHERE", "TORUS", "EXTRUSION", "REVOLUTION",
    "OFFSET", "BSPLINE_SURFACE", "BEZIER_SURFACE",
)
PREDICATE_NAMES = (
    "geom_type", "planar", "curved", "linear", "circular", "elliptical",
    "spherical", "cylindrical", "conical", "toroidal", "closed", "valid",
    "boundary", "manifold_edge", "non_manifold_edge", "has_holes", "has_single_wire", "has_multiple_wires",
    "isolated_vertex", "endpoint_vertex", "convexity",
    "axis_parallel", "axis_perpendicular", "axis_same_direction", "axis_opposite_direction",
    "positive", "negative", "facing_positive", "facing_negative",
    "bbox_contains_origin", "bbox_touch", "center_on_axis",
    "angle_to_axis", "metric_equal", "metric_compare", "metric_range", "count_compare",
    "coplanar_with", "coaxial_with",
)


class Candidate:
    def __init__(
        self,
        ref: FeatureRef,
        center: Any = None,
        geom_type: str = "",
        shape: Any = None,
        direction: Any = None,
        area: float = 0.0,
        length: float = 0.0,
        volume: float = 0.0,
        radius: Optional[float] = None,
        perimeter: float = 0.0,
        bbox: tuple[float, float, float, float, float, float] = (0.0,) * 6,
        closed: Optional[bool] = None,
        valid: Optional[bool] = None,
        vertex_count: int = 0,
        edge_count: int = 0,
        wire_count: int = 0,
        face_count: int = 0,
        shell_count: int = 0,
        solid_count: int = 0,
        hole_count: int = 0,
        adjacent_face_count: int = 0,
        vertex_valence: int = 0,
        source_bbox: tuple[float, float, float, float, float, float] = (0.0,) * 6,
        convexity: Optional[str] = None,
        axis_direction: Any = None,
        axis_point: Any = None,
        _obj: Any = None,
    ):
        self.ref = ref
        self.shape = shape
        self._obj = _obj
        self._center = center
        self._geom_type = geom_type
        self._direction = direction
        self._area = area
        self._length = length
        self._volume = volume
        self._radius = radius
        self._perimeter = perimeter
        self._bbox = bbox if bbox != (0.0,) * 6 else None
        self._closed = closed
        self._valid = valid
        self._vertex_count = vertex_count
        self._edge_count = edge_count
        self._wire_count = wire_count
        self._face_count = face_count
        self._shell_count = shell_count
        self._solid_count = solid_count
        self._hole_count = hole_count
        self.adjacent_face_count = adjacent_face_count
        self.vertex_valence = vertex_valence
        self.source_bbox = source_bbox
        self.convexity = convexity
        self._axis_direction = axis_direction
        self._axis_point = axis_point

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Candidate):
            return False
        return self.ref == other.ref

    def __hash__(self) -> int:
        return hash(self.ref)

    @property
    def center(self) -> Any:
        if self._center is None:
            if self.shape is not None:
                self._center = feature_center(self._obj, self.shape)
            else:
                import FreeCAD as App
                self._center = App.Vector(0, 0, 0)
        return self._center

    @property
    def geom_type(self) -> str:
        if not self._geom_type:
            if self.shape is not None:
                self._geom_type = geometry_type(self.shape)
            else:
                self._geom_type = ""
        return self._geom_type

    @property
    def direction(self) -> Any:
        if self._direction is None and self.shape is not None:
            self._direction = representative_direction(self._obj, self.shape, self.ref.kind)
        return self._direction

    @property
    def area(self) -> float:
        if not self._area and self.shape is not None:
            self._area = feature_area(self.shape)
        return self._area

    @property
    def length(self) -> float:
        if not self._length and self.shape is not None:
            self._length = feature_length(self.shape)
        return self._length

    @property
    def volume(self) -> float:
        if not self._volume and self.shape is not None:
            self._volume = feature_volume(self.shape)
        return self._volume

    @property
    def radius(self) -> Optional[float]:
        if self._radius is None and self.shape is not None:
            self._radius = feature_radius(self.shape)
        return self._radius

    @property
    def perimeter(self) -> float:
        if not self._perimeter and self.shape is not None:
            self._perimeter = feature_perimeter(self.shape)
        return self._perimeter

    @property
    def bbox(self) -> tuple[float, float, float, float, float, float]:
        if self._bbox is None:
            if self.shape is not None:
                self._bbox = bbox_tuple(self._obj, self.shape)
            else:
                self._bbox = (0.0,) * 6
        return self._bbox

    @property
    def closed(self) -> Optional[bool]:
        if self._closed is None and self.shape is not None:
            self._closed = is_closed(self.shape)
        return self._closed

    @property
    def valid(self) -> Optional[bool]:
        if self._valid is None and self.shape is not None:
            self._valid = is_valid(self.shape)
        return self._valid

    @property
    def hole_count(self) -> int:
        if not self._hole_count and self.shape is not None and self.ref.kind == "Face":
            self._hole_count = face_hole_count(self.shape)
        return self._hole_count

    @property
    def axis_direction(self) -> Any:
        if self._axis_direction is None and self.shape is not None:
            self._axis_direction, self._axis_point = feature_axis_info(self._obj, self.shape)
        return self._axis_direction

    @property
    def axis_point(self) -> Any:
        if self._axis_point is None and self.shape is not None:
            self._axis_direction, self._axis_point = feature_axis_info(self._obj, self.shape)
        return self._axis_point

    def _ensure_counts(self) -> None:
        if not self._face_count and not self._edge_count and not self._vertex_count and self.shape is not None:
            vc, ec, wc, fc, shc, soc = _shape_counts(self.shape)
            self._vertex_count, self._edge_count, self._wire_count = vc, ec, wc
            self._face_count, self._shell_count, self._solid_count = fc, shc, soc

    @property
    def vertex_count(self) -> int:
        self._ensure_counts()
        return self._vertex_count

    @property
    def edge_count(self) -> int:
        self._ensure_counts()
        return self._edge_count

    @property
    def wire_count(self) -> int:
        self._ensure_counts()
        return self._wire_count

    @property
    def face_count(self) -> int:
        self._ensure_counts()
        return self._face_count

    @property
    def shell_count(self) -> int:
        self._ensure_counts()
        return self._shell_count

    @property
    def solid_count(self) -> int:
        self._ensure_counts()
        return self._solid_count

    @property
    def diameter(self) -> float:
        return (2.0 * self.radius) if self.radius is not None else float("nan")

    @property
    def distance(self) -> float:
        return math.sqrt(self.center.x ** 2 + self.center.y ** 2 + self.center.z ** 2)

    @property
    def bbox_size_x(self) -> float:
        return self.bbox[3] - self.bbox[0]

    @property
    def bbox_size_y(self) -> float:
        return self.bbox[4] - self.bbox[1]

    @property
    def bbox_size_z(self) -> float:
        return self.bbox[5] - self.bbox[2]

    @property
    def bbox_diagonal(self) -> float:
        return math.sqrt(self.bbox_size_x ** 2 + self.bbox_size_y ** 2 + self.bbox_size_z ** 2)

    @property
    def bbox_volume(self) -> float:
        return max(0.0, self.bbox_size_x) * max(0.0, self.bbox_size_y) * max(0.0, self.bbox_size_z)

    @property
    def bbox_aspect_ratio(self) -> float:
        sizes = [abs(self.bbox_size_x), abs(self.bbox_size_y), abs(self.bbox_size_z)]
        positive = [value for value in sizes if value > EPS]
        if not positive:
            return 1.0
        return max(positive) / min(positive)

    @property
    def compactness(self) -> float:
        if self.perimeter <= EPS or self.area <= EPS:
            return float("nan")
        return 4.0 * math.pi * self.area / (self.perimeter * self.perimeter)

    def _direction_component(self, axis: str) -> float:
        direction = self.direction
        if direction is None:
            return float("nan")
        return float(getattr(direction, axis))

    @property
    def axis_distance_x(self) -> float:
        return math.hypot(self.center.y, self.center.z)

    @property
    def axis_distance_y(self) -> float:
        return math.hypot(self.center.x, self.center.z)

    @property
    def axis_distance_z(self) -> float:
        return math.hypot(self.center.x, self.center.y)

    def metric(self, name: str) -> float:
        if name in ("x", "y", "z"):
            return float(getattr(self.center, name))
        if name == "distance":
            return float(self.distance)
        if name == "length":
            return float(self.length)
        if name == "perimeter":
            return float(self.perimeter)
        if name == "area":
            return float(self.area)
        if name == "compactness":
            return float(self.compactness)
        if name == "volume":
            return float(self.volume)
        if name == "radius":
            return float(self.radius) if self.radius is not None else float("nan")
        if name == "diameter":
            return float(self.diameter)
        if name == "axis_distance_x":
            return float(self.axis_distance_x)
        if name == "axis_distance_y":
            return float(self.axis_distance_y)
        if name == "axis_distance_z":
            return float(self.axis_distance_z)
        if name == "bbox_min_x":
            return float(self.bbox[0])
        if name == "bbox_max_x":
            return float(self.bbox[3])
        if name == "bbox_min_y":
            return float(self.bbox[1])
        if name == "bbox_max_y":
            return float(self.bbox[4])
        if name == "bbox_min_z":
            return float(self.bbox[2])
        if name == "bbox_max_z":
            return float(self.bbox[5])
        if name == "bbox_size_x":
            return float(self.bbox_size_x)
        if name == "bbox_size_y":
            return float(self.bbox_size_y)
        if name == "bbox_size_z":
            return float(self.bbox_size_z)
        if name == "bbox_diagonal":
            return float(self.bbox_diagonal)
        if name == "bbox_volume":
            return float(self.bbox_volume)
        if name == "bbox_aspect_ratio":
            return float(self.bbox_aspect_ratio)
        if name in ("direction_x", "direction_y", "direction_z"):
            return float(self._direction_component(name[-1]))
        if name == "vertex_count":
            return float(self.vertex_count)
        if name == "edge_count":
            return float(self.edge_count)
        if name == "wire_count":
            return float(self.wire_count)
        if name == "face_count":
            return float(self.face_count)
        if name == "shell_count":
            return float(self.shell_count)
        if name == "solid_count":
            return float(self.solid_count)
        if name == "hole_count":
            return float(self.hole_count)
        if name == "adjacent_face_count":
            return float(self.adjacent_face_count)
        if name == "vertex_valence":
            return float(self.vertex_valence)
        raise KeyError(name)


def _shape_counts(shape: Any) -> tuple[int, int, int, int, int, int]:
    def length_of(attr: str) -> int:
        try:
            return len(getattr(shape, attr))
        except (AttributeError, TypeError):
            return 0
    return (
        length_of("Vertexes"), length_of("Edges"), length_of("Wires"),
        length_of("Faces"), length_of("Shells"), length_of("Solids"),
    )


def candidate_for(
    obj: Any,
    subname: str,
    kind: str,
    edge_adjacent: Optional[dict[str, int]] = None,
    vertex_valences: Optional[dict[str, int]] = None,
    source_bbox: tuple[float, float, float, float, float, float] = (0.0,) * 6,
    edge_convexities_map: Optional[dict[str, str]] = None,
    shape: Any = None,
) -> Candidate:
    ref = make_ref(obj, subname, kind)
    if shape is None:
        shape = resolve_ref(obj, ref)
    return Candidate(
        ref=ref,
        shape=shape,
        _obj=obj,
        source_bbox=source_bbox,
        adjacent_face_count=(edge_adjacent or {}).get(subname, 0) if kind == "Edge" else 0,
        vertex_valence=(vertex_valences or {}).get(subname, 0) if kind == "Vertex" else 0,
        convexity=(edge_convexities_map or {}).get(subname) if kind == "Edge" else None,
    )


def candidates(obj: Any, kind: str) -> list[Candidate]:
    if isinstance(obj, list):
        return obj
    if kind not in FEATURE_KINDS:
        raise ValueError(f"Unknown feature kind: {kind}")
    if kind == "Shape":
        return [candidate_for(obj, "", kind)] if has_shape_for_selector(obj) else []
    subfeatures = features_from_object(obj, kind)
    edges = adjacent_face_counts(obj) if kind == "Edge" else None
    conv = edge_convexities(obj) if kind == "Edge" else None
    vertices = adjacent_vertex_counts(obj) if kind == "Vertex" else None
    source_bbox = bbox_tuple(obj, getattr(obj, "Shape", None))
    return [
        candidate_for(obj, f"{kind}{index}", kind, edges, vertices, source_bbox, conv, shape=subfeatures[index - 1])
        for index in range(1, len(subfeatures) + 1)
    ]


def has_shape_for_selector(obj: Any) -> bool:
    shape = getattr(obj, "Shape", None)
    try:
        return shape is not None and not shape.isNull()
    except (AttributeError, TypeError):
        return shape is not None


def _axis(name: str) -> tuple[float, float, float]:
    try:
        return AXES[str(name).upper()]
    except KeyError as exc:
        raise ValueError(f"Unsupported axis: {name!r}") from exc


def _dot(direction: Any, axis: tuple[float, float, float]) -> float:
    if direction is None:
        return float("nan")
    return float(direction.x * axis[0] + direction.y * axis[1] + direction.z * axis[2])


def _direction_relation(direction: Any, axis: tuple[float, float, float], relation: str, tolerance: float) -> bool:
    dot = _dot(direction, axis)
    if not math.isfinite(dot):
        return False
    if relation == "parallel":
        return abs(abs(dot) - 1.0) <= tolerance
    if relation == "perpendicular":
        return abs(dot) <= tolerance
    if relation == "same":
        return dot >= 1.0 - tolerance
    if relation == "opposite":
        return dot <= -1.0 + tolerance
    raise ValueError(relation)


def _compare(actual: float, op: str, expected: float, tolerance: float) -> bool:
    op = str(op).strip().lower()
    if not math.isfinite(actual):
        return False
    if op in {"==", "=", "eq"}:
        return abs(actual - expected) <= tolerance
    if op in {"!=", "ne"}:
        return abs(actual - expected) > tolerance
    if op in {">", "gt"}:
        return actual > expected + tolerance
    if op in {">=", "ge"}:
        return actual >= expected - tolerance
    if op in {"<", "lt"}:
        return actual < expected - tolerance
    if op in {"<=", "le"}:
        return actual <= expected + tolerance
    raise ValueError(f"Unsupported comparison operator: {op!r}")


def _geometry_boolean(geom: str, name: str) -> bool:
    families = {
        "planar": {"PLANE"},
        "curved": set(GEOMETRY_TYPES) - {"PLANE", "LINE"},
        "linear": {"LINE"},
        "circular": {"CIRCLE"},
        "elliptical": {"ELLIPSE"},
        "spherical": {"SPHERE"},
        "cylindrical": {"CYLINDER"},
        "conical": {"CONE"},
        "toroidal": {"TORUS"},
    }
    return geom in families[name]


def predicate(name: str, value: Any, tolerance: float = 1e-6) -> Callable[[Candidate], bool]:
    name = str(name)
    if name == "geom_type":
        expected = str(value).upper()
        return lambda c: c.geom_type == expected
    if name in {"planar", "curved", "linear", "circular", "elliptical", "spherical", "cylindrical", "conical", "toroidal"}:
        expected = bool(value)
        return lambda c: _geometry_boolean(c.geom_type, name) is expected
    if name in {"closed", "valid"}:
        expected = bool(value)
        return lambda c: getattr(c, name) is expected
    if name == "has_holes":
        expected = bool(value)
        return lambda c: (c.hole_count > 0) is expected
    if name == "has_single_wire":
        expected = bool(value)
        return lambda c: (c.wire_count == 1) is expected
    if name == "has_multiple_wires":
        expected = bool(value)
        return lambda c: (c.wire_count > 1) is expected
    if name == "isolated_vertex":
        expected = bool(value)
        return lambda c: (c.vertex_valence == 0) is expected
    if name == "endpoint_vertex":
        expected = bool(value)
        return lambda c: (c.vertex_valence == 1) is expected
    if name == "boundary":
        expected = bool(value)
        return lambda c: (c.adjacent_face_count == 1) is expected
    if name == "manifold_edge":
        expected = bool(value)
        return lambda c: (c.adjacent_face_count == 2) is expected
    if name == "non_manifold_edge":
        expected = bool(value)
        return lambda c: (c.adjacent_face_count not in {0, 1, 2}) is expected
    if name in {"axis_parallel", "axis_perpendicular", "axis_same_direction", "axis_opposite_direction"}:
        axis = _axis(value)
        relation = {
            "axis_parallel": "parallel", "axis_perpendicular": "perpendicular",
            "axis_same_direction": "same", "axis_opposite_direction": "opposite",
        }[name]
        return lambda c: _direction_relation(c.direction, axis, relation, tolerance)
    if name in {"positive", "negative"}:
        axis = _axis(value[0] if isinstance(value, (tuple, list)) else value)
        sign = 1.0 if name == "positive" else -1.0
        return lambda c: sign * _dot(c.center, axis) >= -tolerance
    if name in {"facing_positive", "facing_negative"}:
        axis = _axis(value[0] if isinstance(value, (tuple, list)) else value)
        sign = 1.0 if name == "facing_positive" else -1.0
        return lambda c: sign * _dot(c.direction, axis) >= 1.0 - tolerance
    if name == "bbox_contains_origin":
        expected = bool(value)
        def contains(c: Candidate) -> bool:
            return (
                c.bbox[0] - tolerance <= 0 <= c.bbox[3] + tolerance
                and c.bbox[1] - tolerance <= 0 <= c.bbox[4] + tolerance
                and c.bbox[2] - tolerance <= 0 <= c.bbox[5] + tolerance
            ) is expected
        return contains
    if name == "bbox_touch":
        if not isinstance(value, dict):
            raise ValueError("bbox_touch requires {'axis': ..., 'side': ...}")
        axis = str(value.get("axis", "Z")).upper()
        side = str(value.get("side", "max")).lower()
        touch_tolerance = float(value.get("tolerance", tolerance))
        if axis not in AXES or side not in {"min", "max"}:
            raise ValueError("bbox_touch axis must be X/Y/Z and side must be min/max")
        idx = {"X": (0, 3), "Y": (1, 4), "Z": (2, 5)}[axis][side == "max"]
        return lambda c: abs(c.bbox[idx] - c.source_bbox[idx]) <= touch_tolerance
    if name == "center_on_axis":
        if not isinstance(value, dict):
            raise ValueError("center_on_axis requires {'axis': ...}")
        axis = str(value.get("axis", "Z")).upper()
        axis_tolerance = float(value.get("tolerance", tolerance))
        if axis not in AXES:
            raise ValueError("center_on_axis axis must be X/Y/Z")
        orthogonal = {"X": ("y", "z"), "Y": ("x", "z"), "Z": ("x", "y")}
        a, b = orthogonal[axis]
        return lambda c: abs(getattr(c.center, a)) <= axis_tolerance and abs(getattr(c.center, b)) <= axis_tolerance
    if name == "angle_to_axis":
        if not isinstance(value, dict):
            raise ValueError("angle_to_axis requires {'axis': ..., 'value': degrees}")
        axis = _axis(value.get("axis", "Z"))
        expected = float(value["value"])
        angle_tolerance = float(value.get("tolerance", tolerance))
        return lambda c: math.isfinite(_dot(c.direction, axis)) and abs(math.degrees(math.acos(max(-1.0, min(1.0, abs(_dot(c.direction, axis)))))) - expected) <= angle_tolerance
    if name in {"metric_equal", "metric_compare", "count_compare"}:
        if not isinstance(value, dict):
            raise ValueError(f"{name} requires a dictionary")
        metric = str(value["metric"])
        operator = "==" if name == "metric_equal" else str(value.get("operator", "=="))
        expected = float(value["value"])
        metric_tolerance = float(value.get("tolerance", tolerance))
        return lambda c: _compare(c.metric(metric), operator, expected, metric_tolerance)
    if name == "metric_range":
        if not isinstance(value, dict):
            raise ValueError("metric_range requires a dictionary with 'metric', 'min', and 'max'")
        metric = str(value["metric"])
        min_val = float(value.get("min", -float("inf")))
        max_val = float(value.get("max", float("inf")))
        metric_tolerance = float(value.get("tolerance", tolerance))
        return lambda c: (min_val - metric_tolerance <= c.metric(metric) <= max_val + metric_tolerance)
    if name == "convexity":
        expected = str(value).lower()
        return lambda c: str(getattr(c, "convexity", "") or "").lower() == expected
    if name == "coplanar_with":
        if not isinstance(value, dict):
            raise ValueError("coplanar_with requires {'plane_normal': ..., 'plane_point': ...}")
        norm = tuple(float(x) for x in value["plane_normal"])
        pt = tuple(float(x) for x in value["plane_point"])
        plane_tolerance = float(value.get("tolerance", tolerance))
        def is_coplanar(c: Candidate) -> bool:
            if c.geom_type != "PLANE" or c.direction is None:
                return False
            dot_n = abs(c.direction.x * norm[0] + c.direction.y * norm[1] + c.direction.z * norm[2])
            if abs(dot_n - 1.0) > plane_tolerance:
                return False
            dist = abs((c.center.x - pt[0]) * norm[0] + (c.center.y - pt[1]) * norm[1] + (c.center.z - pt[2]) * norm[2])
            return dist <= plane_tolerance
        return is_coplanar
    if name == "coaxial_with":
        if not isinstance(value, dict):
            raise ValueError("coaxial_with requires {'axis_direction': ..., 'axis_point': ...}")
        d_ref = tuple(float(x) for x in value["axis_direction"])
        p_ref = tuple(float(x) for x in value["axis_point"])
        axis_tolerance = float(value.get("tolerance", tolerance))
        def is_coaxial(c: Candidate) -> bool:
            d_c = c.axis_direction
            p_c = c.axis_point or c.center
            if d_c is None:
                return False
            dot = abs(d_c.x * d_ref[0] + d_c.y * d_ref[1] + d_c.z * d_ref[2])
            if abs(dot - 1.0) > axis_tolerance:
                return False
            delta = (p_c.x - p_ref[0], p_c.y - p_ref[1], p_c.z - p_ref[2])
            cx = delta[1] * d_ref[2] - delta[2] * d_ref[1]
            cy = delta[2] * d_ref[0] - delta[0] * d_ref[2]
            cz = delta[0] * d_ref[1] - delta[1] * d_ref[0]
            dist = math.sqrt(cx * cx + cy * cy + cz * cz)
            return dist <= axis_tolerance
        return is_coaxial
    raise ValueError(f"Unknown predicate: {name}")


@dataclass(frozen=True)
class Step:
    op: str
    args: dict[str, Any] = field(default_factory=dict)
    forced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "args": self.args, "forced": self.forced}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Step":
        return Step(str(data["op"]), dict(data.get("args", {})), bool(data.get("forced", False)))

    def label(self) -> str:
        a = self.args
        if self.op == "filter":
            name = str(a.get("name", "filter"))
            value = a.get("value")
            if name == "convexity":
                return f"Edge Convexity = {str(value).title()}"
            if name == "metric_range" and isinstance(value, dict):
                return f"{str(value.get('metric', 'Metric')).replace('_', ' ').title()} in range [{value.get('min', '-∞')}, {value.get('max', '+∞')}]"
            if name == "coplanar_with":
                return "Coplanar with reference plane"
            if name == "coaxial_with":
                return "Coaxial with reference axis"
            if name in {"metric_equal", "metric_compare", "count_compare"} and isinstance(value, dict):
                op = "≈" if name == "metric_equal" else value.get("operator", "=")
                return f"{name.replace('_', ' ').title()} {value.get('metric')} {op} {value.get('value')}"
            if name == "angle_to_axis" and isinstance(value, dict):
                return f"Angle to {value.get('axis', 'Z')} ≈ {value.get('value')}°"
            if name == "bbox_touch" and isinstance(value, dict):
                return f"Touches source bounding box {value.get('axis', 'Z')}{'+' if value.get('side', 'max') == 'max' else '-'}"
            if name == "center_on_axis" and isinstance(value, dict):
                return f"Center on {value.get('axis', 'Z')} axis"
            if name.startswith("axis_"):
                return f"{name.replace('_', ' ').title()} {value}"
            return f"Filter {name.replace('_', ' ')} = {value}"
        if self.op == "extreme":
            metric = str(a.get("metric"))
            direction = str(a.get("direction", "max"))
            special = {
                ("x", "max"): "Furthest X+", ("x", "min"): "Furthest X-",
                ("y", "max"): "Furthest Y+", ("y", "min"): "Furthest Y-",
                ("z", "max"): "Furthest Z+", ("z", "min"): "Furthest Z-",
                ("distance", "max"): "Furthest from origin", ("distance", "min"): "Nearest origin",
                ("length", "max"): "Longest", ("length", "min"): "Shortest",
                ("area", "max"): "Most area", ("area", "min"): "Least area",
                ("volume", "max"): "Largest volume", ("volume", "min"): "Smallest volume",
                ("radius", "max"): "Largest radius", ("radius", "min"): "Smallest radius",
                ("bbox_max_z", "max"): "Highest bounding Z", ("bbox_min_z", "min"): "Lowest bounding Z",
                ("vertex_valence", "max"): "Highest vertex valence", ("vertex_valence", "min"): "Lowest vertex valence",
                ("adjacent_face_count", "max"): "Most adjacent faces", ("adjacent_face_count", "min"): "Fewest adjacent faces",
                ("axis_distance_x", "min"): "Closest to X axis", ("axis_distance_x", "max"): "Furthest from X axis",
                ("axis_distance_y", "min"): "Closest to Y axis", ("axis_distance_y", "max"): "Furthest from Y axis",
                ("axis_distance_z", "min"): "Closest to Z axis", ("axis_distance_z", "max"): "Furthest from Z axis",
                ("bbox_volume", "max"): "Largest bounding box", ("bbox_volume", "min"): "Smallest bounding box",
            }
            return special.get((metric, direction), f"Extreme {direction} {metric}")
        if self.op == "sort_take":
            direction = "descending" if a.get("direction", "max") == "max" else "ascending"
            return f"Sort {a.get('metric')} {direction} → take {a.get('count', 1)}"
        if self.op == "take":
            return f"Take first {a.get('count', 1)}"
        if self.op == "skip":
            return f"Skip first {a.get('count', 1)}"
        return self.op.replace("_", " ").title()


@dataclass(frozen=True)
class Selector:
    kind: str
    steps: tuple[Step, ...] = ()
    expected_count: Optional[int] = None
    name: str = ""
    version: int = 3
    expression: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "steps": [s.to_dict() for s in self.steps],
            "expected_count": self.expected_count,
            "name": self.name,
            "version": self.version,
            "expression": self.expression,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def from_json(text: str) -> "Selector":
        data = json.loads(text)
        version = int(data.get("version", 1))
        if version not in {1, 2, 3}:
            raise ValueError(f"Unsupported selector query version {version}")
        steps = tuple(Step.from_dict(x) for x in data.get("steps", []))
        kind = str(data["kind"])
        expr = str(data.get("expression", ""))
        return Selector(
            kind=kind,
            steps=steps,
            expected_count=data.get("expected_count"),
            name=str(data.get("name", "")),
            version=3 if version < 3 else version,
            expression=expr,
        )

    def describe(self) -> str:
        if self.expression:
            return self.expression
        if self.steps:
            from fs_expression import steps_to_expression
            return steps_to_expression(self.steps, self.kind)
        return f"All {self.kind.lower()}s"

    def evaluate_candidates(self, obj: Any) -> list[Candidate]:
        if self.steps:
            current = obj if isinstance(obj, list) else candidates(obj, self.kind)
            for step in self.steps:
                current = apply_step(current, step)
            return current
        if self.expression:
            from fs_expression import evaluate_expression
            refs = evaluate_expression(obj, self.expression, kind=self.kind)
            if isinstance(obj, (list, tuple)) and obj:
                sub_set = {r.subname for r in refs}
                return [c for c in obj if getattr(getattr(c, "ref", None), "subname", "") in sub_set]
            return [candidate_for(obj, r.subname, r.kind) for r in refs]
        return obj if isinstance(obj, list) else candidates(obj, self.kind)

    def evaluate(self, obj: Any) -> list[FeatureRef]:
        current = self.evaluate_candidates(obj)
        refs = [candidate.ref for candidate in current]
        if self.expected_count is not None and len(refs) != int(self.expected_count):
            return []
        return refs

    def is_exact_count(self, obj: Any) -> bool:
        return self.expected_count is None or len(self.evaluate_candidates(obj)) == int(self.expected_count)


def apply_step(items: list[Candidate], step: Step) -> list[Candidate]:
    op = str(step.op)
    args = step.args
    if op == "filter":
        fn = predicate(str(args["name"]), args.get("value"), float(args.get("tolerance", 1e-6)))
        return [c for c in items if fn(c)]
    if op == "extreme":
        if not items:
            return []
        metric = str(args["metric"])
        direction = str(args.get("direction", "max"))
        if direction not in {"min", "max"}:
            raise ValueError("extreme direction must be 'min' or 'max'")
        values = [c.metric(metric) for c in items]
        finite = [v for v in values if math.isfinite(v)]
        if not finite:
            return []
        extremum = max(finite) if direction == "max" else min(finite)
        tolerance = float(args.get("tolerance", 1e-6))
        return [
            c for c in items
            if math.isfinite(c.metric(metric)) and abs(c.metric(metric) - extremum) <= tolerance
        ]
    if op == "sort_take":
        if not items:
            return []
        metric = str(args["metric"])
        direction = str(args.get("direction", "max"))
        count = max(0, int(args.get("count", 1)))
        if any(not math.isfinite(c.metric(metric)) for c in items):
            return []
        ordered = sorted(items, key=lambda c: c.metric(metric), reverse=direction == "max")
        return ordered[:count]
    if op == "take":
        return items[:max(0, int(args.get("count", 1)))]
    if op == "skip":
        return items[max(0, int(args.get("count", 1))):]
    raise ValueError(f"Unknown selector op: {op}")


OPERATION_KINDS = ("filter", "extreme", "sort_take")


def selector_to_python(selector: Selector, obj_var: str = "obj") -> str:
    """Generate self-contained, reproducible FreeCAD Python code for this selector."""
    lines = [
        "# Generated by FreeCAD FeatureSelector",
        "from fs_selector import Selector, Step",
        "",
        "selector = Selector(",
        f'    kind="{selector.kind}",',
        f"    expected_count={repr(selector.expected_count)},",
        "    steps=(",
    ]
    for step in selector.steps:
        lines.append(f'        Step("{step.op}", {repr(step.args)}),')
    lines.extend([
        "    ),",
        ")",
        f"# Evaluate against {obj_var}:",
        f"refs = selector.evaluate({obj_var})",
        f"subnames = [r.subname for r in refs]",
        f"print(f'Selected {{len(subnames)}} {selector.kind.lower()}s:', subnames)",
    ])
    return "\n".join(lines)

