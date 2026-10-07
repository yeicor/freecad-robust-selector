"""CadQuery-style selector expression language for FreeCAD FeatureSelector.

Provides a rich, high-performance expression parser and evaluator following
CadQuery selector semantics (https://cadquery.readthedocs.io/en/latest/selectors.html),
expanded to support component descending and cursor-based autocompletion.
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence, Union

try:
    import FreeCAD as App
except ImportError:
    App = None

from fs_freecad import FeatureRef, shape_type_from_subname
from fs_selector import GeometryItem, candidates, candidate_for

EvaluatorItem = GeometryItem


def evaluate_scalar_value(val: Any, obj: Any = None, tolerance: float = 1e-4) -> float:
    """Evaluate a numeric scalar literal or FreeCAD parametric expression.

    Supports:
      - Numeric types: int, float, Base.Quantity
      - Numeric literal strings: "5.0", "-12.3", "10"
      - FreeCAD expressions with leading '=': "=VarSet.param", "=Spreadsheet.B2", "=Pad.Length / 2"
      - Bare dotted FreeCAD identifiers: "VarSet.HoleRadius", "Spreadsheet.drill_dia"
      - Unit arithmetic expressions: "=10mm + 2mm", "=VarSet.r * 2"
    """
    if isinstance(val, (int, float)):
        return float(val)
    if hasattr(val, "Value"):
        return float(val.Value)

    val_str = str(val).strip()
    if not val_str:
        return 0.0

    clean_val = val_str.lstrip("=")
    try:
        return float(clean_val)
    except ValueError:
        pass

    eval_target = None
    if obj is not None:
        if hasattr(obj, "evalExpression"):
            eval_target = obj
        elif hasattr(obj, "Document") and obj.Document:
            eval_target = next((o for o in obj.Document.Objects if hasattr(o, "evalExpression")), None)

    if eval_target is None and App is not None and getattr(App, "ActiveDocument", None):
        eval_target = next((o for o in App.ActiveDocument.Objects if hasattr(o, "evalExpression")), None)

    if eval_target is not None:
        try:
            res = eval_target.evalExpression(clean_val)
            if hasattr(res, "Value"):
                return float(res.Value)
            return float(res)
        except Exception:
            pass

    try:
        node = ast.parse(clean_val, mode="eval")

        def _eval_node(n):
            if isinstance(n, ast.Expression):
                return _eval_node(n.body)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
                return float(n.value)
            if isinstance(n, ast.Name):
                if eval_target and hasattr(eval_target, "evalExpression"):
                    try:
                        res = eval_target.evalExpression(n.id)
                        return float(getattr(res, "Value", res))
                    except Exception:
                        pass
                if obj and hasattr(obj, n.id):
                    v = getattr(obj, n.id)
                    return float(getattr(v, "Value", v))
            if isinstance(n, ast.Attribute):
                attr_expr = ast.unparse(n) if hasattr(ast, "unparse") else f"{getattr(n.value, 'id', '')}.{n.attr}"
                if eval_target and hasattr(eval_target, "evalExpression"):
                    try:
                        res = eval_target.evalExpression(attr_expr)
                        return float(getattr(res, "Value", res))
                    except Exception:
                        pass
                if isinstance(n.value, ast.Name):
                    target_o = getattr(obj, n.value.id, None)
                    if target_o is None and hasattr(obj, "Document") and obj.Document:
                        target_o = obj.Document.getObject(n.value.id)
                    if target_o and hasattr(target_o, n.attr):
                        v = getattr(target_o, n.attr)
                        return float(getattr(v, "Value", v))
            if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
                op_val = _eval_node(n.operand)
                return op_val if isinstance(n.op, ast.UAdd) else -op_val
            if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
                l = _eval_node(n.left)
                r = _eval_node(n.right)
                if isinstance(n.op, ast.Add): return l + r
                if isinstance(n.op, ast.Sub): return l - r
                if isinstance(n.op, ast.Mult): return l * r
                if isinstance(n.op, ast.Div): return l / r
                if isinstance(n.op, ast.Pow): return l ** r
            raise ValueError(f"Unsupported node: {type(n)}")

        return _eval_node(node)
    except Exception:
        pass

    raise ValueError(f"Cannot evaluate scalar expression: {val_str!r}")


METRIC_ALIASES: dict[str, str] = {
    "rad": "radius",
    "r": "radius",
    "dia": "diameter",
    "d": "diameter",
    "len": "length",
    "l": "length",
    "perim": "perimeter",
    "vol": "volume",
    "dist": "distance",
}

KNOWN_METRICS = {
    "x", "y", "z", "distance", "length", "perimeter", "area", "volume",
    "radius", "diameter", "compactness", "axis_distance_x", "axis_distance_y",
    "axis_distance_z", "hole_count", "adjacent_face_count", "vertex_valence",
}


# ---------------------------------------------------------------------------
#  Vector math helpers
# ---------------------------------------------------------------------------

def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _length(a: tuple[float, float, float]) -> float:
    return math.hypot(a[0], a[1], a[2])


def _normalize(a: tuple[float, float, float]) -> tuple[float, float, float]:
    l = _length(a)
    return (a[0] / l, a[1] / l, a[2] / l) if l > 1e-12 else (0.0, 0.0, 0.0)


def _angle(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    na, nb = _normalize(a), _normalize(b)
    d = max(-1.0, min(1.0, _dot(na, nb)))
    return math.acos(d)


STANDARD_AXES: dict[str, tuple[float, float, float]] = {
    "X": (1.0, 0.0, 0.0),
    "Y": (0.0, 1.0, 0.0),
    "Z": (0.0, 0.0, 1.0),
    "-X": (-1.0, 0.0, 0.0),
    "-Y": (0.0, -1.0, 0.0),
    "-Z": (0.0, 0.0, -1.0),
    "+X": (1.0, 0.0, 0.0),
    "+Y": (0.0, 1.0, 0.0),
    "+Z": (0.0, 0.0, 1.0),
    "XY": _normalize((1.0, 1.0, 0.0)),
    "YZ": _normalize((0.0, 1.0, 1.0)),
    "XZ": _normalize((1.0, 0.0, 1.0)),
}


def parse_vector(token: str) -> tuple[float, float, float]:
    clean = token.strip()
    up = clean.upper()
    if up in STANDARD_AXES:
        return STANDARD_AXES[up]
    # Check parenthesized vector e.g. (1, 0, 0)
    match = re.match(r"^\(?\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*\)?$", clean)
    if match:
        return _normalize((float(match.group(1)), float(match.group(2)), float(match.group(3))))
    raise ValueError(f"Unknown direction or vector: {token!r}")


# ---------------------------------------------------------------------------
#  CadQuery Selector Filters
# ---------------------------------------------------------------------------

class Filter:
    """Base selector filter."""

    def apply(self, items: Sequence[GeometryItem]) -> list[GeometryItem]:
        return list(items)

    def describe(self) -> str:
        return ""


class DirectionMinMaxFilter(Filter):
    """>Axis or <Axis (farthest / minimum in direction vector)."""

    def __init__(self, direction: tuple[float, float, float], is_max: bool = True, tolerance: float = 1e-4):
        self.direction = direction
        self.is_max = is_max
        self.tolerance = tolerance

    def apply(self, items: Sequence[GeometryItem]) -> list[GeometryItem]:
        if not items:
            return []
        scores = [_dot(item.center_tuple, self.direction) for item in items]
        extreme = max(scores) if self.is_max else min(scores)
        return [item for item, score in zip(items, scores) if abs(score - extreme) <= self.tolerance]

    def describe(self) -> str:
        dir_name = next((k for k, v in STANDARD_AXES.items() if v == self.direction), str(self.direction))
        return f"{'>' if self.is_max else '<'}{dir_name}"


class MetricClusterFilter(Filter):
    """>>metric[N] or <<metric[N] or >>metric[start:end] (clustering equal/epsilon values)."""

    def __init__(
        self,
        metric: str,
        selector: Union[int, str, slice] = "0",
        is_descending: bool = True,
        tolerance: float = 1e-4,
    ):
        self.metric = metric
        self.selector = selector
        self.is_descending = is_descending
        self.tolerance = tolerance

    def apply(self, items: Sequence[GeometryItem]) -> list[GeometryItem]:
        if not items:
            return []
        m_norm = METRIC_ALIASES.get(self.metric.lower(), self.metric.lower())
        is_axis = self.metric.upper() in STANDARD_AXES

        scored: list[tuple[GeometryItem, float]] = []
        for it in items:
            if is_axis:
                vec = STANDARD_AXES[self.metric.upper()]
                score = _dot(it.center_tuple, vec)
            else:
                try:
                    score = it.metric(m_norm)
                except (KeyError, AttributeError):
                    score = float("nan")
            if math.isfinite(score):
                scored.append((it, score))

        if not scored:
            return []

        scored.sort(key=lambda p: p[1], reverse=self.is_descending)

        clusters: list[list[GeometryItem]] = []
        last_val: Optional[float] = None
        for it, val in scored:
            if last_val is None or abs(val - last_val) > self.tolerance:
                clusters.append([it])
                last_val = val
            else:
                clusters[-1].append(it)

        if not clusters:
            return []

        sel = self.selector
        if isinstance(sel, int):
            idx = sel if sel >= 0 else len(clusters) + sel
            return clusters[idx] if 0 <= idx < len(clusters) else []

        if isinstance(sel, slice):
            selected_clusters = clusters[sel]
            return [it for cl in selected_clusters for it in cl]

        raw_sel = str(sel).strip()
        sel_lower = raw_sel.lower()
        if not raw_sel or raw_sel == "0":
            return clusters[0]
        if sel_lower in ("largest", "max_count"):
            return max(clusters, key=len)
        if sel_lower in ("unique", "single"):
            return [it for cl in clusters if len(cl) == 1 for it in cl]
        if sel_lower in ("all_equal", "equal"):
            return [it for cl in clusters for it in cl] if len(clusters) == 1 else []

        base_obj = items[0].base_obj if items and hasattr(items[0], "base_obj") else None

        for comp_sym in ("!=", "==", ">=", "<=", ">", "<"):
            if raw_sel.startswith(comp_sym):
                rhs = raw_sel[len(comp_sym):].strip()
                try:
                    target_v = evaluate_scalar_value(rhs or 0.0, base_obj, tolerance=self.tolerance)
                    if comp_sym == "!=":
                        return [it for cl in clusters for it in cl if abs(it.metric(self.metric) - target_v) > self.tolerance]
                    if comp_sym == "==":
                        return [it for cl in clusters for it in cl if abs(it.metric(self.metric) - target_v) <= self.tolerance]
                    if comp_sym == ">=":
                        return [it for cl in clusters for it in cl if it.metric(self.metric) >= target_v - self.tolerance]
                    if comp_sym == ">":
                        return [it for cl in clusters for it in cl if it.metric(self.metric) > target_v + self.tolerance]
                    if comp_sym == "<=":
                        return [it for cl in clusters for it in cl if it.metric(self.metric) <= target_v + self.tolerance]
                    if comp_sym == "<":
                        return [it for cl in clusters for it in cl if it.metric(self.metric) < target_v - self.tolerance]
                except (ValueError, TypeError):
                    pass

        # Direct expression index / target value e.g. =VarSet.tier or =Spreadsheet.A1
        if raw_sel.startswith("=") or ("." in raw_sel and not raw_sel.replace(".", "", 1).isdigit()):
            try:
                target_v = evaluate_scalar_value(raw_sel, base_obj, tolerance=self.tolerance)
                # First check if target_v matches a cluster's metric value (e.g. radius == 5.0)
                matched_cl = next((cl for cl in clusters if abs(cl[0].metric(self.metric) - target_v) <= self.tolerance), None)
                if matched_cl is not None:
                    return matched_cl
                # Else check if it's an integer tier index
                if target_v.is_integer():
                    idx = int(target_v)
                    idx = idx if idx >= 0 else len(clusters) + idx
                    return clusters[idx] if 0 <= idx < len(clusters) else []
            except Exception:
                pass

        if ":" in raw_sel:
            parts = raw_sel.split(":")
            start = int(parts[0]) if parts[0] else None
            stop = int(parts[1]) if parts[1] else None
            step = int(parts[2]) if len(parts) > 2 and parts[2] else None
            selected_clusters = clusters[slice(start, stop, step)]
            return [it for cl in selected_clusters for it in cl]
        try:
            idx = int(raw_sel)
            idx = idx if idx >= 0 else len(clusters) + idx
            return clusters[idx] if 0 <= idx < len(clusters) else []
        except ValueError:
            return []

    def describe(self) -> str:
        prefix = ">>" if self.is_descending else "<<"
        sel_repr = self.selector if not isinstance(self.selector, slice) else f"{self.selector.start or ''}:{self.selector.stop or ''}"
        name = self.metric.upper() if self.metric.upper() in STANDARD_AXES else self.metric
        return f"{prefix}{name}[{sel_repr}]"


class CenterNthFilter(MetricClusterFilter):
    """>>Axis[N] or <<Axis[N] extremum cluster filter."""

    def __init__(self, direction: tuple[float, float, float], n: int = 0, is_max: bool = True, tolerance: float = 1e-4):
        dir_name = next((k for k, v in STANDARD_AXES.items() if v == direction), "Z")
        super().__init__(dir_name, selector=n, is_descending=is_max, tolerance=tolerance)


class DirectionFilter(Filter):
    """+Axis or -Axis (normal or tangent aligned with direction vector)."""

    def __init__(self, direction: tuple[float, float, float], tolerance: float = 1e-4):
        self.direction = direction
        self.tolerance = tolerance

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        result = []
        for item in items:
            vec = item.normal if item.kind == "Face" else item.tangent
            if vec is not None and _angle(vec, self.direction) <= self.tolerance:
                result.append(item)
        return result

    def describe(self) -> str:
        dir_name = next((k for k, v in STANDARD_AXES.items() if v == self.direction), str(self.direction))
        return f"+{dir_name}"


class ParallelFilter(Filter):
    """|Axis (normal or tangent parallel to direction vector)."""

    def __init__(self, direction: tuple[float, float, float], tolerance: float = 1e-4):
        self.direction = direction
        self.tolerance = tolerance

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        result = []
        for item in items:
            vec = item.normal if item.kind == "Face" else item.tangent
            if vec is not None and _length(_cross(vec, self.direction)) <= self.tolerance:
                result.append(item)
        return result

    def describe(self) -> str:
        dir_name = next((k for k, v in STANDARD_AXES.items() if v == self.direction), str(self.direction))
        return f"|{dir_name}"


class PerpendicularFilter(Filter):
    """#Axis (normal or tangent orthogonal to direction vector)."""

    def __init__(self, direction: tuple[float, float, float], tolerance: float = 1e-4):
        self.direction = direction
        self.tolerance = tolerance

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        result = []
        for item in items:
            vec = item.normal if item.kind == "Face" else item.tangent
            if vec is not None and abs(_dot(vec, self.direction)) <= self.tolerance:
                result.append(item)
        return result

    def describe(self) -> str:
        dir_name = next((k for k, v in STANDARD_AXES.items() if v == self.direction), str(self.direction))
        return f"#{dir_name}"


TYPE_NORM = {
    "PLANAR": "PLANE",
    "PLANE": "PLANE",
    "LINEAR": "LINE",
    "LINE": "LINE",
    "CIRCULAR": "CIRCLE",
    "CIRCLE": "CIRCLE",
    "CYLINDRICAL": "CYLINDER",
    "CYLINDER": "CYLINDER",
    "SPHERICAL": "SPHERE",
    "SPHERE": "SPHERE",
    "CONICAL": "CONE",
    "CONE": "CONE",
    "TOROIDAL": "TORUS",
    "TORUS": "TORUS",
}


class TypeFilter(Filter):
    """%Plane, %Cylinder, %Line, %Circle, etc."""

    def __init__(self, geom_type: str):
        raw = geom_type.strip().upper()
        self.geom_type = TYPE_NORM.get(raw, raw)

    def apply(self, items: Sequence[GeometryItem]) -> list[GeometryItem]:
        target = self.geom_type
        return [
            item for item in items
            if TYPE_NORM.get(item.geom_type.upper(), item.geom_type.upper()) == target
            or item.geom_type.upper().endswith(target)
        ]

    def describe(self) -> str:
        return f"%{self.geom_type.capitalize()}"


class TagFilter(Filter):
    """:concave, :convex, :smooth, :seam, :boundary, :closed, :planar, etc."""

    def __init__(self, tag: str):
        self.tag = tag.lower().strip().lstrip(":")

    def apply(self, items: Sequence[GeometryItem]) -> list[GeometryItem]:
        t = self.tag
        if t in ("concave", "internal"):
            return [it for it in items if getattr(it, "convexity", None) == "concave"]
        if t in ("convex", "external"):
            return [it for it in items if getattr(it, "convexity", None) == "convex"]
        if t == "smooth":
            return [it for it in items if getattr(it, "convexity", None) == "smooth"]
        if t == "closed":
            return [it for it in items if bool(getattr(it, "closed", False))]
        if t in ("boundary", "outer"):
            return [it for it in items if getattr(it, "adjacent_face_count", 0) == 1]
        if t == "manifold":
            return [it for it in items if getattr(it, "adjacent_face_count", 0) == 2]
        if t == "seam":
            return [it for it in items if getattr(it, "is_seam", False) or (getattr(it, "adjacent_face_count", 0) == 1 and it.kind == "Edge")]
        if t in ("hole", "inner"):
            return [it for it in items if getattr(it, "is_hole", False) or getattr(it, "hole_count", 0) > 0]
        if t == "planar":
            return [it for it in items if "PLANE" in getattr(it, "geom_type", "")]
        if t == "cylindrical":
            return [it for it in items if "CYLINDER" in getattr(it, "geom_type", "")]
        if t == "circular":
            return [it for it in items if "CIRCLE" in getattr(it, "geom_type", "")]
        if t == "linear":
            return [it for it in items if "LINE" in getattr(it, "geom_type", "")]
        if t == "spherical":
            return [it for it in items if "SPHERE" in getattr(it, "geom_type", "")]
        if t == "conical":
            return [it for it in items if "CONE" in getattr(it, "geom_type", "")]
        if t == "toroidal":
            return [it for it in items if "TORUS" in getattr(it, "geom_type", "")]
        return []

    def describe(self) -> str:
        return f":{self.tag}"


class NumericCompareFilter(Filter):
    """metric == val, metric > val, etc. Supports FreeCAD expressions (e.g. '=VarSet.param')."""

    def __init__(self, metric: str, op: str, value: Any, tolerance: float = 1e-4):
        self.metric = METRIC_ALIASES.get(metric.lower(), metric.lower())
        self.op = op
        self.value = value
        self.tolerance = tolerance

    def apply(self, items: Sequence[GeometryItem]) -> list[GeometryItem]:
        if not items:
            return []
        base_obj = items[0].base_obj if hasattr(items[0], "base_obj") else None
        try:
            target_val = evaluate_scalar_value(self.value, base_obj, tolerance=self.tolerance)
        except Exception:
            return []

        res = []
        for it in items:
            try:
                v = it.metric(self.metric)
            except (KeyError, AttributeError):
                continue
            if not math.isfinite(v):
                continue
            if self.op == "==" and abs(v - target_val) <= self.tolerance:
                res.append(it)
            elif self.op == "!=" and abs(v - target_val) > self.tolerance:
                res.append(it)
            elif self.op == ">" and v > target_val + self.tolerance:
                res.append(it)
            elif self.op == ">=" and v >= target_val - self.tolerance:
                res.append(it)
            elif self.op == "<" and v < target_val - self.tolerance:
                res.append(it)
            elif self.op == "<=" and v <= target_val + self.tolerance:
                res.append(it)
        return res

    def describe(self) -> str:
        return f"{self.metric} {self.op} {self.value}"


class NumericRangeFilter(Filter):
    """min_val <= metric <= max_val or metric in [min_val, max_val]. Supports '=VarSet.param'."""

    def __init__(self, metric: str, min_val: Any, max_val: Any, tolerance: float = 1e-4):
        self.metric = METRIC_ALIASES.get(metric.lower(), metric.lower())
        self.min_val = min_val
        self.max_val = max_val
        self.tolerance = tolerance

    def apply(self, items: Sequence[GeometryItem]) -> list[GeometryItem]:
        if not items:
            return []
        base_obj = items[0].base_obj if hasattr(items[0], "base_obj") else None
        try:
            min_v = evaluate_scalar_value(self.min_val, base_obj, tolerance=self.tolerance)
            max_v = evaluate_scalar_value(self.max_val, base_obj, tolerance=self.tolerance)
        except Exception:
            return []

        res = []
        for it in items:
            try:
                v = it.metric(self.metric)
            except (KeyError, AttributeError):
                continue
            if math.isfinite(v) and (min_v - self.tolerance <= v <= max_v + self.tolerance):
                res.append(it)
        return res

    def describe(self) -> str:
        return f"{self.min_val} <= {self.metric} <= {self.max_val}"


class SubnameFilter(Filter):
    """Matches a specific subelement name like Face1, Edge2, Vertex3."""

    def __init__(self, target_subname: str):
        self.target_subname = target_subname

    def apply(self, items: Sequence[GeometryItem]) -> list[GeometryItem]:
        return [it for it in items if it.subname.lower() == self.target_subname.lower()]

    def describe(self) -> str:
        return self.target_subname


class AdjacentFilter(Filter):
    """adjacent_to(sub_expression): elements touching/sharing topology with reference."""

    def __init__(self, target_expr: str, tolerance: float = 1e-4):
        self.target_expr = target_expr
        self.tolerance = tolerance

    def apply(self, items: Sequence[GeometryItem]) -> list[GeometryItem]:
        if not items:
            return []
        base_obj = items[0].base_obj
        if base_obj is None:
            return list(items)

        ref_kind = None
        m_sub = re.match(r"^(Face|Edge|Vertex)\d+$", self.target_expr.strip(), re.IGNORECASE)
        if m_sub:
            ref_kind = m_sub.group(1).capitalize()

        ref_items = evaluate_expression_items(base_obj, self.target_expr, kind=ref_kind, tolerance=self.tolerance)
        if not ref_items:
            return []
        ref_shapes = [ri.shape for ri in ref_items if ri.shape is not None]
        ref_subnames = {ri.subname.lower() for ri in ref_items}

        matched = []
        for it in items:
            if it.shape is None:
                continue
            if it.subname.lower() in ref_subnames:
                continue
            for rs in ref_shapes:
                try:
                    if hasattr(it.shape, "distToShape"):
                        dist, _pts, _sol = it.shape.distToShape(rs)
                        if dist <= self.tolerance:
                            matched.append(it)
                            break
                    elif hasattr(rs, "isSame") and (rs.isSame(it.shape) or rs.isPartner(it.shape)):
                        matched.append(it)
                        break
                except Exception:
                    continue
        return matched

    def describe(self) -> str:
        return f"adjacent_to({self.target_expr})"


class CoplanarFilter(Filter):
    """coplanar_to(sub_expression): planar faces coplanar with reference face(s)."""

    def __init__(self, target_expr: str, tolerance: float = 1e-4):
        self.target_expr = target_expr
        self.tolerance = tolerance

    def apply(self, items: Sequence[GeometryItem]) -> list[GeometryItem]:
        if not items:
            return []
        base_obj = items[0].base_obj
        if base_obj is None:
            return list(items)

        ref_kind = "Face"
        m_sub = re.match(r"^(Face|Edge|Vertex)\d+$", self.target_expr.strip(), re.IGNORECASE)
        if m_sub:
            ref_kind = m_sub.group(1).capitalize()

        ref_items = evaluate_expression_items(base_obj, self.target_expr, kind=ref_kind, tolerance=self.tolerance)
        planes = []
        for ri in ref_items:
            if ri.normal is not None:
                planes.append((ri.center_tuple, ri.normal))
        if not planes:
            return []

        matched = []
        for it in items:
            if it.kind != "Face" or it.normal is None:
                continue
            for pt, norm in planes:
                dot = abs(_dot(it.normal, norm))
                if abs(dot - 1.0) <= self.tolerance:
                    c = it.center_tuple
                    dist = abs((c[0] - pt[0]) * norm[0] + (c[1] - pt[1]) * norm[1] + (c[2] - pt[2]) * norm[2])
                    if dist <= self.tolerance:
                        matched.append(it)
                        break
        return matched

    def describe(self) -> str:
        return f"coplanar_to({self.target_expr})"


class CoaxialFilter(Filter):
    """coaxial_to(sub_expression): cylinders/circles sharing central axis."""

    def __init__(self, target_expr: str, tolerance: float = 1e-4):
        self.target_expr = target_expr
        self.tolerance = tolerance

    def apply(self, items: Sequence[GeometryItem]) -> list[GeometryItem]:
        if not items:
            return []
        base_obj = items[0].base_obj
        if base_obj is None:
            return list(items)

        ref_kind = None
        m_sub = re.match(r"^(Face|Edge|Vertex)\d+$", self.target_expr.strip(), re.IGNORECASE)
        if m_sub:
            ref_kind = m_sub.group(1).capitalize()

        ref_items = evaluate_expression_items(base_obj, self.target_expr, kind=ref_kind, tolerance=self.tolerance)
        axes = []
        for ri in ref_items:
            ad = getattr(ri, "axis_direction", None)
            ap = getattr(ri, "axis_point", None) or ri.center
            if ad is not None:
                axes.append(((float(ad.x), float(ad.y), float(ad.z)), (float(ap.x), float(ap.y), float(ap.z))))
        if not axes:
            return []

        matched = []
        for it in items:
            iad = getattr(it, "axis_direction", None)
            iap = getattr(it, "axis_point", None) or it.center
            if iad is None:
                continue
            d_it = (float(iad.x), float(iad.y), float(iad.z))
            p_it = (float(iap.x), float(iap.y), float(iap.z))
            for d_ref, p_ref in axes:
                dot = abs(_dot(d_it, d_ref))
                if abs(dot - 1.0) <= self.tolerance:
                    delta = (p_it[0] - p_ref[0], p_it[1] - p_ref[1], p_it[2] - p_ref[2])
                    cross_d = _cross(delta, d_ref)
                    if _length(cross_d) <= self.tolerance:
                        matched.append(it)
                        break
        return matched

    def describe(self) -> str:
        return f"coaxial_to({self.target_expr})"


class NamedViewFilter(Filter):
    """Named view: top, bottom, front, back, left, right."""

    def __init__(self, name: str):
        self.name = name.lower()
        mapping: dict[str, tuple[tuple[float, float, float], bool]] = {
            "top": ((0.0, 0.0, 1.0), True),
            "bottom": ((0.0, 0.0, 1.0), False),
            "front": ((0.0, -1.0, 0.0), True),
            "back": ((0.0, 1.0, 0.0), True),
            "left": ((-1.0, 0.0, 0.0), True),
            "right": ((1.0, 0.0, 0.0), True),
        }
        direction, is_max = mapping.get(self.name, ((0.0, 0.0, 1.0), True))
        self.inner = DirectionMinMaxFilter(direction, is_max)

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        return self.inner.apply(items)

    def describe(self) -> str:
        return self.name


class AndFilter(Filter):
    """Logical intersection: left and right."""

    def __init__(self, left: Filter, right: Filter):
        self.left = left
        self.right = right

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        left_res = self.left.apply(items)
        left_subnames = {item.subname for item in left_res}
        right_res = self.right.apply(left_res)
        return [item for item in right_res if item.subname in left_subnames]

    def describe(self) -> str:
        return f"{self.left.describe()} and {self.right.describe()}"


class OrFilter(Filter):
    """Logical union: left or right."""

    def __init__(self, left: Filter, right: Filter):
        self.left = left
        self.right = right

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        left_res = self.left.apply(items)
        right_res = self.right.apply(items)
        seen = set()
        merged = []
        for item in left_res + right_res:
            if item.subname not in seen:
                seen.add(item.subname)
                merged.append(item)
        return merged

    def describe(self) -> str:
        return f"{self.left.describe()} or {self.right.describe()}"


class NotFilter(Filter):
    """Logical negation / complement: not child."""

    def __init__(self, child: Filter):
        self.child = child

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        excluded = {item.subname for item in self.child.apply(items)}
        return [item for item in items if item.subname not in excluded]

    def describe(self) -> str:
        return f"not ({self.child.describe()})"


class ExceptFilter(Filter):
    """Set difference: left exc right."""

    def __init__(self, left: Filter, right: Filter):
        self.left = left
        self.right = right

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        left_res = self.left.apply(items)
        right_subnames = {item.subname for item in self.right.apply(items)}
        return [item for item in left_res if item.subname not in right_subnames]

    def describe(self) -> str:
        return f"{self.left.describe()} exc {self.right.describe()}"


# ---------------------------------------------------------------------------
#  Pipeline Step & Fluent Descending Chains
# ---------------------------------------------------------------------------

KINDS_MAP = {
    "faces": "Face",
    "face": "Face",
    "edges": "Edge",
    "edge": "Edge",
    "vertices": "Vertex",
    "vertex": "Vertex",
    "vertexes": "Vertex",
    "wires": "Wire",
    "wire": "Wire",
    "solids": "Solid",
    "solid": "Solid",
    "shells": "Shell",
    "shell": "Shell",
}


class PipelineStep:
    """One step in a selector chain (e.g. faces('>Z') or edges('<X'))."""

    def __init__(self, kind: str, filter_obj: Optional[Filter] = None):
        self.kind = kind
        self.filter_obj = filter_obj

    def describe(self) -> str:
        method = f"{self.kind.lower()}s"
        if not self.filter_obj or not self.filter_obj.describe():
            return f"{method}()"
        return f'{method}("{self.filter_obj.describe()}")'


class ExpressionPipeline:
    """Full chained pipeline of selector steps."""

    def __init__(self, steps: list[PipelineStep]):
        self.steps = steps

    @property
    def target_kind(self) -> str:
        return self.steps[-1].kind if self.steps else "Face"

    def describe(self) -> str:
        if not self.steps:
            return ""
        if len(self.steps) == 1 and self.steps[0].filter_obj:
            # Clean shorthand if single step without explicit method
            desc = self.steps[0].filter_obj.describe()
            if desc and not desc.startswith("All"):
                return desc
        return ".".join(step.describe() for step in self.steps)


# ---------------------------------------------------------------------------
#  CadQuery Expression Tokenizer and Parser
# ---------------------------------------------------------------------------

def _tokenize_filter(expr: str) -> list[str]:
    """Tokenize a filter string into tokens."""
    token_spec = [
        ("FC_EXPR", r"=[A-Za-z0-9_][A-Za-z0-9_.]*(?:\s*[\+\-\*\/]\s*[A-Za-z0-9_.]+)*"),
        ("NUMBER", r"-?\d+(?:\.\d+)?"),
        ("AXIS_VEC", r"\([+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?\)"),
        ("OP2", r">>|<<"),
        ("COMP_OP", r"==|!=|<=|>=|<|>"),
        ("TAG", r":[A-Za-z_][A-Za-z0-9_]*"),
        ("SLICE_COLON", r":"),
        ("OP1", r"[><|#%+\-]"),
        ("DOTTED_NAME", r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"),
        ("WORD", r"[A-Za-z_][A-Za-z0-9_]*"),
        ("LBRACK", r"\["),
        ("RBRACK", r"\]"),
        ("LPAREN", r"\("),
        ("RPAREN", r"\)"),
        ("COMMA", r","),
        ("QUOTE", r"[\"']"),
        ("WS", r"\s+"),
    ]
    regex = "|".join(f"(?P<{name}>{pattern})" for name, pattern in token_spec)
    tokens: list[str] = []
    pos = 0
    while pos < len(expr):
        m = re.match(regex, expr[pos:])
        if not m:
            tokens.append(expr[pos])
            pos += 1
            continue
        kind = m.lastgroup
        val = m.group(kind)
        pos += len(val)
        if kind != "WS":
            tokens.append(val)
    return tokens


def _is_scalar_token(tok: str) -> bool:
    """Check if token is a numeric literal or FreeCAD parameter expression."""
    if not tok:
        return False
    if tok.startswith("="):
        return True
    try:
        float(tok)
        return True
    except ValueError:
        pass
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$", tok):
        return True
    return False


def _parse_filter_atom(tokens: list[str], idx: int, tolerance: float = 1e-4) -> tuple[Filter, int]:
    """Parse a primary filter atom from tokens[idx:]."""
    if idx >= len(tokens):
        raise ValueError("Unexpected end of expression")

    tok = tokens[idx]

    # Parenthesized subexpression: ( ... )
    if tok == "(":
        inner, next_idx = _parse_filter_or(tokens, idx + 1, tolerance=tolerance)
        if next_idx < len(tokens) and tokens[next_idx] == ")":
            next_idx += 1
        return inner, next_idx

    # Quoted string literal: "..."
    if tok in ('"', "'"):
        next_idx = idx + 1
        str_tokens = []
        while next_idx < len(tokens) and tokens[next_idx] != tok:
            str_tokens.append(tokens[next_idx])
            next_idx += 1
        if next_idx < len(tokens):
            next_idx += 1
        inner_filter, _ = _parse_filter_or(str_tokens, 0, tolerance=tolerance)
        return inner_filter, next_idx

    # Topological & Convexity Tags: :concave, :convex, :smooth, etc.
    if tok.startswith(":"):
        return TagFilter(tok[1:]), idx + 1

    # Combinators: adjacent_to(...), coplanar_to(...), coaxial_to(...)
    if tok.lower() in ("adjacent_to", "coplanar_to", "coaxial_to"):
        comb_name = tok.lower()
        next_idx = idx + 1
        if next_idx < len(tokens) and tokens[next_idx] == "(":
            next_idx += 1
            inner_tokens = []
            depth = 1
            while next_idx < len(tokens):
                t = tokens[next_idx]
                if t == "(":
                    depth += 1
                elif t == ")":
                    depth -= 1
                    if depth == 0:
                        next_idx += 1
                        break
                inner_tokens.append(t)
                next_idx += 1
            inner_expr = " ".join(inner_tokens)
            if (inner_expr.startswith('"') and inner_expr.endswith('"')) or (inner_expr.startswith("'") and inner_expr.endswith("'")):
                inner_expr = inner_expr[1:-1].strip()
            if comb_name == "adjacent_to":
                return AdjacentFilter(inner_expr, tolerance=tolerance), next_idx
            if comb_name == "coplanar_to":
                return CoplanarFilter(inner_expr, tolerance=tolerance), next_idx
            if comb_name == "coaxial_to":
                return CoaxialFilter(inner_expr, tolerance=tolerance), next_idx

    # Named views
    if tok.lower() in ("top", "bottom", "front", "back", "left", "right"):
        return NamedViewFilter(tok.lower()), idx + 1

    # Type selector: %Plane, %Cylinder, etc.
    if tok == "%":
        idx += 1
        if idx >= len(tokens):
            raise ValueError("Expected type name after '%'")
        type_name = tokens[idx]
        return TypeFilter(type_name), idx + 1
    if tok.startswith("%") and len(tok) > 1:
        return TypeFilter(tok[1:]), idx + 1

    # Numeric range comparisons: min_val <= metric <= max_val or min_val < metric < max_val
    if _is_scalar_token(tok) and idx + 4 < len(tokens) and tokens[idx+1] in ("<", "<=") and tokens[idx+3] in ("<", "<="):
        metric_tok = tokens[idx+2]
        norm_metric = METRIC_ALIASES.get(metric_tok.lower(), metric_tok.lower())
        if norm_metric in KNOWN_METRICS or metric_tok.lower() in KNOWN_METRICS:
            min_v: Any = float(tok) if re.match(r"^-?\d+(?:\.\d+)?$", tok) else tok
            max_tok = tokens[idx+4]
            max_v: Any = float(max_tok) if re.match(r"^-?\d+(?:\.\d+)?$", max_tok) else max_tok
            return NumericRangeFilter(norm_metric, min_v, max_v, tolerance=tolerance), idx + 5

    # Numeric comparisons: metric == value, metric > value, metric in [min, max]
    norm_metric = METRIC_ALIASES.get(tok.lower(), tok.lower())
    if norm_metric in KNOWN_METRICS or tok.lower() in KNOWN_METRICS:
        if idx + 2 < len(tokens) and tokens[idx+1] in ("==", "!=", "<", "<=", ">", ">="):
            op = tokens[idx+1]
            val_tok = tokens[idx+2]
            if _is_scalar_token(val_tok):
                val: Any = float(val_tok) if re.match(r"^-?\d+(?:\.\d+)?$", val_tok) else val_tok
                return NumericCompareFilter(norm_metric, op, val, tolerance=tolerance), idx + 3
        if idx + 5 < len(tokens) and tokens[idx+1].lower() == "in" and tokens[idx+2] == "[":
            min_tok = tokens[idx+3]
            max_tok = tokens[idx+5]
            if _is_scalar_token(min_tok) and _is_scalar_token(max_tok):
                min_v = float(min_tok) if re.match(r"^-?\d+(?:\.\d+)?$", min_tok) else min_tok
                max_v = float(max_tok) if re.match(r"^-?\d+(?:\.\d+)?$", max_tok) else max_tok
                next_i = idx + 6
                if next_i < len(tokens) and tokens[next_i] == "]":
                    next_i += 1
                return NumericRangeFilter(norm_metric, min_v, max_v, tolerance=tolerance), next_i

    # Universal Metric Clustering (>>metric[slice] or <<metric[slice])
    if tok in (">>=", ">>", "<<") or tok.startswith((">>", "<<")):
        op = ">>" if tok.startswith(">>") else "<<"
        remainder = tok[2:].strip()
        if remainder:
            target_metric = remainder
            idx += 1
        else:
            idx += 1
            if idx >= len(tokens):
                raise ValueError(f"Expected metric or axis after {op}")
            target_metric = tokens[idx]
            idx += 1

        selector_str = "0"
        if idx < len(tokens) and tokens[idx] == "[":
            idx += 1
            bracket_tokens = []
            while idx < len(tokens) and tokens[idx] != "]":
                bracket_tokens.append(tokens[idx])
                idx += 1
            if idx < len(tokens) and tokens[idx] == "]":
                idx += 1
            selector_str = "".join(bracket_tokens)

        return MetricClusterFilter(target_metric, selector_str, is_descending=(op == ">>"), tolerance=tolerance), idx

    # Direction / Extrema / Parallel / Perpendicular operators
    # Cases: >Z, <Z, |Z, #Z, +Z, -Z
    op = ""
    if tok in (">", "<", "|", "#", "+", "-"):
        op = tok
        idx += 1
        if idx >= len(tokens):
            raise ValueError(f"Expected axis or vector after operator {op!r}")
        axis_tok = tokens[idx]
        idx += 1
    else:
        # Check combined token like ">Z" or "|X" or "+Z"
        m = re.match(r"^([><|#+\-])(.*)$", tok)
        if m:
            op = m.group(1)
            axis_tok = m.group(2)
            idx += 1
            if not axis_tok and idx < len(tokens):
                axis_tok = tokens[idx]
                idx += 1
        else:
            # Standalone axis word like "Z" -> default to DirectionMinMax >Z
            if tok.upper() in STANDARD_AXES:
                op = ">"
                axis_tok = tok
                idx += 1
            else:
                # Subelement name token: Face1, Edge4, Vertex2
                subname_match = re.match(r"^(Face|Edge|Vertex)(\d+)$", tok, re.IGNORECASE)
                if subname_match:
                    sub_name = f"{subname_match.group(1).capitalize()}{subname_match.group(2)}"
                    return SubnameFilter(sub_name), idx + 1
                raise ValueError(f"Unrecognized selector token: {tok!r}")

    vec = parse_vector(axis_tok)

    # Check for optional [index] e.g. >Z[0] or >Z[-1]
    index_val: Optional[int] = None
    if idx < len(tokens) and tokens[idx] == "[":
        idx += 1
        if idx < len(tokens):
            try:
                index_val = int(tokens[idx])
                idx += 1
            except ValueError:
                pass
        if idx < len(tokens) and tokens[idx] == "]":
            idx += 1

    if index_val is not None:
        is_max = op in (">", "+")
        return CenterNthFilter(vec, index_val, is_max=is_max), idx

    if op == ">":
        return DirectionMinMaxFilter(vec, is_max=True), idx
    if op == "<":
        return DirectionMinMaxFilter(vec, is_max=False), idx
    if op == "+":
        return DirectionFilter(vec), idx
    if op == "-":
        neg_vec = (-vec[0], -vec[1], -vec[2])
        return DirectionFilter(neg_vec), idx
    if op == "|":
        return ParallelFilter(vec), idx
    if op == "#":
        return PerpendicularFilter(vec), idx

    return DirectionMinMaxFilter(vec, is_max=True), idx


def _parse_filter_not(tokens: list[str], idx: int, tolerance: float = 1e-4) -> tuple[Filter, int]:
    """Parse 'not' unary operator."""
    if idx < len(tokens) and tokens[idx].lower() == "not":
        child, next_idx = _parse_filter_not(tokens, idx + 1, tolerance=tolerance)
        return NotFilter(child), next_idx
    return _parse_filter_atom(tokens, idx, tolerance=tolerance)


def _parse_filter_and(tokens: list[str], idx: int, tolerance: float = 1e-4) -> tuple[Filter, int]:
    """Parse 'and' / '&' / '+' intersection."""
    left, idx = _parse_filter_not(tokens, idx, tolerance=tolerance)
    while idx < len(tokens):
        op = tokens[idx].lower()
        if op in ("and", "&") or (op == "+" and idx + 1 < len(tokens) and tokens[idx + 1] in (">", "<", "|", "#", "%")):
            right, idx = _parse_filter_not(tokens, idx + 1, tolerance=tolerance)
            left = AndFilter(left, right)
        elif op in ("exc", "except"):
            right, idx = _parse_filter_not(tokens, idx + 1, tolerance=tolerance)
            left = ExceptFilter(left, right)
        else:
            break
    return left, idx


def _parse_filter_or(tokens: list[str], idx: int, tolerance: float = 1e-4) -> tuple[Filter, int]:
    """Parse 'or' / '|' union."""
    left, idx = _parse_filter_and(tokens, idx, tolerance=tolerance)
    while idx < len(tokens):
        op = tokens[idx].lower()
        if op == "or" or (op == "|" and idx + 1 < len(tokens) and tokens[idx + 1] not in STANDARD_AXES):
            right, idx = _parse_filter_and(tokens, idx + 1, tolerance=tolerance)
            left = OrFilter(left, right)
        else:
            break
    return left, idx


def parse_filter_string(expr: str, tolerance: float = 1e-4) -> Filter:
    """Parse a boolean CadQuery filter string into a Filter object."""
    clean = expr.strip()
    if not clean:
        return Filter()
    tokens = _tokenize_filter(clean)
    if not tokens:
        return Filter()
    flt, _ = _parse_filter_or(tokens, 0, tolerance=tolerance)
    return flt


def parse_expression(expr_str: str, default_kind: str = "Face", tolerance: float = 1e-4) -> ExpressionPipeline:
    """Parse a full CadQuery selector expression (including method chaining).

    Supports:
      - `faces(">Z").edges("<X")`
      - `faces(>Z).edges(<X)`
      - `>Z` (bare filter expression; defaults to default_kind)
      - `|Z and >Y`
      - `solids().faces(">Z")`
      - `faces(">Z") -> edges("<X")`
    """
    clean = expr_str.strip()
    if not clean:
        return ExpressionPipeline([])

    # Split into pipeline stages along '.' or '->' or '>>' outside parentheses and quotes
    stages: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote: Optional[str] = None
    i = 0
    while i < len(clean):
        c = clean[i]
        if in_quote:
            if c == in_quote:
                in_quote = None
            current.append(c)
        elif c in ('"', "'"):
            in_quote = c
            current.append(c)
        elif c in ('(', '['):
            depth += 1
            current.append(c)
        elif c in (')', ']'):
            depth = max(0, depth - 1)
            current.append(c)
        elif depth == 0 and c == '.' and re.match(r"^\.[A-Za-z_][A-Za-z0-9_]*\s*\(", clean[i:]):
            stages.append("".join(current).strip())
            current = []
        elif depth == 0 and clean[i:i+2] == "->":
            stages.append("".join(current).strip())
            current = []
            i += 1  # Skip second character
        else:
            current.append(c)
        i += 1
    if current:
        stages.append("".join(current).strip())

    steps: list[PipelineStep] = []
    for stage in stages:
        stage = stage.strip()
        if not stage:
            continue
        # Check method call: faces(...) or edges(...)
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)$", stage, re.DOTALL)
        if match and match.group(1).lower() in KINDS_MAP:
            method_name = match.group(1).lower()
            inner_arg = match.group(2).strip()
            # Strip enclosing quotes if present
            if (inner_arg.startswith('"') and inner_arg.endswith('"')) or (inner_arg.startswith("'") and inner_arg.endswith("'")):
                inner_arg = inner_arg[1:-1].strip()
            kind = KINDS_MAP.get(method_name, method_name.capitalize())
            filter_obj = parse_filter_string(inner_arg, tolerance=tolerance) if inner_arg else None
            steps.append(PipelineStep(kind, filter_obj))
        else:
            # Check bare subelement name: Face1, Edge2, Vertex3
            match_sub = re.match(r"^(Face|Edge|Vertex)(\d+)$", stage.strip(), re.IGNORECASE)
            if match_sub:
                inferred_kind = match_sub.group(1).capitalize()
                sub_name = f"{inferred_kind}{match_sub.group(2)}"
                steps.append(PipelineStep(inferred_kind, SubnameFilter(sub_name)))
            else:
                # Standalone filter expression without method name
                filter_obj = parse_filter_string(stage, tolerance=tolerance)
                steps.append(PipelineStep(default_kind, filter_obj))

    return ExpressionPipeline(steps)


# ---------------------------------------------------------------------------
#  Evaluation Engine
# ---------------------------------------------------------------------------

def _extract_subshapes(shape: Any, kind: str) -> list[Any]:
    """Extract raw subshapes of a given kind from a FreeCAD shape."""
    if shape is None:
        return []
    if kind == "Face":
        return list(getattr(shape, "Faces", []))
    if kind == "Edge":
        return list(getattr(shape, "Edges", []))
    if kind == "Vertex":
        return list(getattr(shape, "Vertexes", []))
    if kind == "Wire":
        return list(getattr(shape, "Wires", []))
    if kind == "Solid":
        return list(getattr(shape, "Solids", []))
    if kind == "Shell":
        return list(getattr(shape, "Shells", []))
    return [shape]


def evaluate_expression_items(
    obj: Any,
    expr_str: str,
    kind: Optional[str] = None,
    tolerance: float = 1e-4,
) -> list[EvaluatorItem]:
    """Evaluate expression against obj and return list of EvaluatorItem."""
    if isinstance(obj, (list, tuple)):
        if not obj:
            return []
        first = obj[0]
        base_obj = getattr(first, "obj", None)
        if base_obj is None and hasattr(first, "ref"):
            ref = getattr(first, "ref", None)
            doc = getattr(App, "ActiveDocument", None) if "App" in globals() and App is not None else None
            if doc and ref and getattr(ref, "object_name", None):
                base_obj = doc.getObject(ref.object_name)
        if base_obj is not None:
            obj = base_obj
        else:
            cand_shape = getattr(first, "shape", None)
            if cand_shape is not None:
                obj = cand_shape

    shape = getattr(obj, "Shape", obj)
    if shape is None or (hasattr(shape, "isNull") and shape.isNull()):
        return []

    default_kind = kind or "Face"
    pipeline = parse_expression(expr_str, default_kind=default_kind, tolerance=tolerance)
    if not pipeline.steps:
        # Empty expression returns all items of default kind
        try:
            return candidates(obj, default_kind)
        except Exception:
            raw = _extract_subshapes(shape, default_kind)
            return [EvaluatorItem(s, f"{default_kind}{idx+1}", default_kind, obj) for idx, s in enumerate(raw)]

    current_items: list[EvaluatorItem] = []
    for step_idx, step in enumerate(pipeline.steps):
        if step_idx == 0:
            try:
                current_items = candidates(obj, step.kind)
            except Exception:
                raw_subshapes = _extract_subshapes(shape, step.kind)
                current_items = [
                    EvaluatorItem(s, f"{step.kind}{idx+1}", step.kind, obj)
                    for idx, s in enumerate(raw_subshapes)
                ]
        else:
            # Descend from previous matching shapes to their child subshapes
            next_raw: list[Any] = []
            for item in current_items:
                for sub in _extract_subshapes(item.shape, step.kind):
                    next_raw.append(sub)

            # Map child subshapes to base object candidates or subname indices
            try:
                base_cands = candidates(obj, step.kind)
            except Exception:
                base_cands = []

            de_duped: list[EvaluatorItem] = []
            seen_indices = set()
            if base_cands:
                for sub in next_raw:
                    matched = next((b for b in base_cands if b.shape is not None and b.shape.isSame(sub)), None)
                    if matched is not None and matched.subname not in seen_indices:
                        seen_indices.add(matched.subname)
                        de_duped.append(matched)
            else:
                all_base = _extract_subshapes(shape, step.kind)
                for sub in next_raw:
                    matched_idx = next((i for i, b in enumerate(all_base) if b.isSame(sub)), None)
                    if matched_idx is not None and matched_idx not in seen_indices:
                        seen_indices.add(matched_idx)
                        de_duped.append(EvaluatorItem(sub, f"{step.kind}{matched_idx+1}", step.kind, obj))
            current_items = de_duped

        if step.filter_obj:
            current_items = step.filter_obj.apply(current_items)

    return current_items


def evaluate_expression(
    obj: Any,
    expr_str: str,
    kind: Optional[str] = None,
    tolerance: float = 1e-4,
) -> list[FeatureRef]:
    """Evaluate expression against obj and return list of FeatureRef."""
    items = evaluate_expression_items(obj, expr_str, kind, tolerance=tolerance)
    obj_name = getattr(obj, "Name", "") if hasattr(obj, "Name") else ""
    return [FeatureRef(obj_name, item.subname, item.kind) for item in items]


# ---------------------------------------------------------------------------
#  Rapid Autocompletion & Cursor-based Planning
# ---------------------------------------------------------------------------

FAST_DISCRIMINATORS = [
    # Primary Extrema
    ">Z", "<Z", ">X", "<X", ">Y", "<Y",
    ">>Z", "<<Z", ">>X", "<<X", ">>Y", "<<Y",
    # Cardinal Directions / Normals / Tangents
    "+Z", "-Z", "+X", "-X", "+Y", "-Y",
    "|Z", "|X", "|Y",
    "#Z", "#X", "#Y",
    # Tags & Geometric Classes
    ":planar", ":cylindrical", ":circular", ":linear",
    ":concave", ":convex", ":smooth", ":closed", ":boundary", ":hole",
    # Cluster / Metric Extrema
    ">>radius[0]", "<<radius[0]",
    ">>length[0]", "<<length[0]",
    ">>area[0]", "<<area[0]",
    # Types
    "%Plane", "%Cylinder", "%Line", "%Circle", "%Sphere", "%Cone",
    # Combined filters
    ">Z and :planar", "<Z and :planar",
    "|Z and >Y", "|Z and <Y", "|Z and >X", "|Z and <X",
    "|Z and :cylindrical",
]


def autocomplete_at_cursor(
    obj: Any,
    target_subnames: list[str],
    target_kind: str,
    expr_str: str,
    cursor_pos: int,
) -> tuple[str, int]:
    """Generate autocompletion text to insert at cursor_pos in expr_str.

    Returns:
        (text_to_insert, cursor_offset)
    """
    if not target_subnames or obj is None:
        return ("", 0)

    target_set = set(target_subnames)
    prefix = expr_str[:cursor_pos]
    suffix = expr_str[cursor_pos:]

    # Check if cursor is inside an unclosed method argument, e.g. "faces(" or "edges("
    unclosed_match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*[\"']?([^)\"']*)$", prefix)
    is_inside_method = bool(unclosed_match)
    method_name = unclosed_match.group(1).lower() if unclosed_match else ""
    scope_kind = KINDS_MAP.get(method_name, target_kind) if is_inside_method else target_kind

    # Check if preceded by a logical operator
    after_and = bool(re.search(r"\band\s*$", prefix))
    after_or = bool(re.search(r"\bor\s*$", prefix))

    # Evaluate context: what items are in scope at cursor?
    base_shape = getattr(obj, "Shape", obj)
    if base_shape is None:
        return ("", 0)

    # 1. If prefix contains higher-order step e.g. "faces('>Z').edges("
    chain_prefix_match = re.search(r"^(.*?)\.([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*[\"']?$", prefix)
    if chain_prefix_match:
        parent_expr = chain_prefix_match.group(1)
        parent_items = evaluate_expression_items(obj, parent_expr)
        pool = []
        try:
            from .fs_selector import candidates
            base_cands = candidates(obj, scope_kind)
        except Exception:
            base_cands = []
        all_target_kind_shapes = _extract_subshapes(base_shape, scope_kind)
        seen_indices = set()
        for p in parent_items:
            for s in _extract_subshapes(p.shape, scope_kind):
                matched = next((b for b in base_cands if b.shape is not None and b.shape.isSame(s)), None) if base_cands else None
                if matched is not None and matched.subname not in seen_indices:
                    seen_indices.add(matched.subname)
                    pool.append(matched)
                elif matched is None:
                    idx = next((i for i, b in enumerate(all_target_kind_shapes) if b.isSame(s)), None)
                    if idx is not None and idx not in seen_indices:
                        seen_indices.add(idx)
                        pool.append(EvaluatorItem(s, f"{scope_kind}{idx+1}", scope_kind, obj))
    else:
        try:
            from .fs_selector import candidates
            pool = candidates(obj, scope_kind)
        except Exception:
            raw = _extract_subshapes(base_shape, scope_kind)
            pool = [EvaluatorItem(s, f"{scope_kind}{idx+1}", scope_kind, obj) for idx, s in enumerate(raw)]

    # 2. Test fast discriminators on the current pool
    best_candidate: Optional[str] = None
    best_score = float("inf")
    exact_candidate: Optional[str] = None

    for disc in FAST_DISCRIMINATORS:
        try:
            flt = parse_filter_string(disc)
            matched = flt.apply(pool)
            matched_subnames = {item.subname for item in matched}
            if not matched_subnames:
                continue

            if matched_subnames == target_set:
                exact_candidate = disc
                break

            # If not exact match, check how closely it pushes towards target
            if target_set.issubset(matched_subnames):
                # Keeps all target items: score is number of extra items
                score = len(matched_subnames) - len(target_set)
                if score < best_score:
                    best_score = score
                    best_candidate = disc
            elif after_or or not expr_str.strip():
                # For OR branches or partial covers: count target coverage
                common = target_set.intersection(matched_subnames)
                if common:
                    score = (len(target_set) - len(common)) * 100 + (len(matched_subnames) - len(common))
                    if score < best_score:
                        best_score = score
                        best_candidate = disc
        except Exception:
            continue

    chosen = exact_candidate or best_candidate

    # 3. If target is Edge or Vertex and no exact filter worked at root: test descending
    if exact_candidate is None and not is_inside_method and (expr_str.strip() == "" or after_or) and scope_kind in ("Edge", "Vertex"):
        # Find which Face contains all (or most) target items
        face_items = evaluate_expression_items(obj, "faces()")
        all_edges = _extract_subshapes(base_shape, scope_kind)
        target_shapes = [all_edges[int(name.replace(scope_kind, "")) - 1] for name in target_subnames if name.startswith(scope_kind)]

        for f_disc in (">Z", "<Z", ">X", "<X", ">Y", "<Y", "|Z", "%Plane", "%Cylinder"):
            try:
                flt_f = parse_filter_string(f_disc)
                m_faces = flt_f.apply(face_items)
                if not m_faces:
                    continue
                # Collect edges of these faces
                edges_of_faces = []
                for mf in m_faces:
                    edges_of_faces.extend(_extract_subshapes(mf.shape, scope_kind))
                # Check if all target shapes belong to these faces
                if all(any(e.isSame(ts) for e in edges_of_faces) for ts in target_shapes):
                    # Test edge filter on these edges
                    edge_pool = [
                        EvaluatorItem(e, f"{scope_kind}{i+1}", scope_kind, obj)
                        for i, e in enumerate(all_edges)
                        if any(e.isSame(ef) for ef in edges_of_faces)
                    ]
                    for e_disc in (">Z", "<Z", ">X", "<X", ">Y", "<Y", "|Z", "#Z"):
                        m_edges = parse_filter_string(e_disc).apply(edge_pool)
                        if {item.subname for item in m_edges} == target_set:
                            chosen = f'faces("{f_disc}").{scope_kind.lower()}s("{e_disc}")'
                            break
                    if chosen:
                        break
                    chosen = f'faces("{f_disc}").{scope_kind.lower()}s()'
                    break
            except Exception:
                continue

    # 4. Fallback if still not found: Nth index or first reasonable filter
    if not chosen:
        chosen = ">Z"

    # 5. Format string for insertion at cursor
    quote = '"' if is_inside_method and not prefix.endswith(('"', "'")) else ""
    close_paren = ")" if is_inside_method and not suffix.startswith(")") else ""

    text_to_insert = f"{quote}{chosen}{quote}{close_paren}" if quote else chosen
    return (text_to_insert, len(text_to_insert))


# ---------------------------------------------------------------------------
#  Step Sequence to Selector Expression Projection
# ---------------------------------------------------------------------------

def steps_to_expression(steps: Sequence[Any], kind: str) -> str:
    """Convert Steps sequence to canonical CadQuery selector expression."""
    if not steps:
        return f"{kind.lower()}s()" if kind != "Shape" else ""

    parts = []
    for step in steps:
        op = getattr(step, "op", "")
        args = getattr(step, "args", {})
        if op == "extreme":
            metric = str(args.get("metric", "z")).upper()
            direction = str(args.get("direction", "max"))
            sym = ">" if direction == "max" else "<"
            parts.append(f"{sym}{metric}")
        elif op == "filter":
            name = str(args.get("name", ""))
            val = args.get("value")
            if name in ("planar", "plane"):
                parts.append("%Plane")
            elif name in ("cylindrical", "cylinder"):
                parts.append("%Cylinder")
            elif name in ("linear", "line"):
                parts.append("%Line")
            elif name in ("circular", "circle"):
                parts.append("%Circle")
            elif name in ("spherical", "sphere"):
                parts.append("%Sphere")
            elif name in ("conical", "cone"):
                parts.append("%Cone")
            elif name in ("toroidal", "torus"):
                parts.append("%Torus")
            elif name == "axis_parallel":
                parts.append(f"|{str(val).upper()}")
            elif name == "axis_perpendicular":
                parts.append(f"#{str(val).upper()}")
            elif name in ("positive", "facing_positive"):
                parts.append(f"+{str(val).upper()}")
            elif name in ("negative", "facing_negative"):
                parts.append(f"-{str(val).upper()}")
            elif name == "geom_type":
                parts.append(f"%{str(val).capitalize()}")
        elif op == "sort_take":
            metric = str(args.get("metric", "z")).upper()
            direction = str(args.get("direction", "max"))
            sym = ">" if direction == "max" else "<"
            count = int(args.get("count", 1))
            parts.append(f"{sym}{metric}[0:{count}]" if count > 1 else f"{sym}{metric}[0]")

    return " and ".join(parts) if parts else f"{kind.lower()}s()"


# ---------------------------------------------------------------------------
#  Synchronized Expression Clause Decomposition & 3D Color Mapping
# ---------------------------------------------------------------------------

@dataclass
class ExpressionClause:
    """Discrete semantic clause of a selector expression mapped to a syntax range and 3D color."""
    text: str
    start_pos: int
    end_pos: int
    color_index: int
    matched_subnames: set[str] = field(default_factory=set)


SYNCHRONIZED_PALETTE: list[dict[str, Any]] = [
    {
        "name": "Cyan",
        "hex": "#00bcd4",
        "rgba_3d": (0.0, 0.737, 0.831, 0.0),
        "bg_editor": "rgba(0, 188, 212, 0.18)",
        "border_editor": "#00bcd4",
        "text_dark": "#00838f",
    },
    {
        "name": "Amber",
        "hex": "#ff9800",
        "rgba_3d": (1.0, 0.596, 0.0, 0.0),
        "bg_editor": "rgba(255, 152, 0, 0.18)",
        "border_editor": "#ff9800",
        "text_dark": "#e65100",
    },
    {
        "name": "Purple",
        "hex": "#ab47bc",
        "rgba_3d": (0.671, 0.278, 0.737, 0.0),
        "bg_editor": "rgba(171, 71, 188, 0.18)",
        "border_editor": "#ab47bc",
        "text_dark": "#6a1b9a",
    },
    {
        "name": "Teal",
        "hex": "#26a69a",
        "rgba_3d": (0.149, 0.651, 0.604, 0.0),
        "bg_editor": "rgba(38, 166, 154, 0.18)",
        "border_editor": "#26a69a",
        "text_dark": "#00695c",
    },
    {
        "name": "Pink",
        "hex": "#ec407a",
        "rgba_3d": (0.925, 0.251, 0.478, 0.0),
        "bg_editor": "rgba(236, 64, 122, 0.18)",
        "border_editor": "#ec407a",
        "text_dark": "#ad1457",
    },
    {
        "name": "Indigo",
        "hex": "#5c6bc0",
        "rgba_3d": (0.361, 0.420, 0.753, 0.0),
        "bg_editor": "rgba(92, 107, 192, 0.18)",
        "border_editor": "#5c6bc0",
        "text_dark": "#283593",
    },
]

OVERLAP_COLOR: dict[str, Any] = {
    "name": "Emerald",
    "hex": "#43a047",
    "rgba_3d": (0.263, 0.627, 0.278, 0.0),
    "bg_editor": "rgba(67, 160, 71, 0.22)",
    "border_editor": "#43a047",
    "text_dark": "#2e7d32",
}


def decompose_clauses(full_text: str) -> list[ExpressionClause]:
    """Decompose selector expression into discrete semantic clauses with character offsets."""
    text = full_text.strip()
    if not text:
        return []

    kinds_set = {"faces", "edges", "vertices", "wires", "solids"}
    m_wrap = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*([\"'])(.*)\2\s*\)$", text, re.DOTALL)
    if m_wrap and m_wrap.group(1).lower() in kinds_set:
        inner = m_wrap.group(3)
        base_offset = full_text.find(inner)
    else:
        inner = text
        base_offset = full_text.find(text)

    clauses: list[ExpressionClause] = []
    depth = 0
    bracket_depth = 0
    in_quote = None
    start = 0
    i = 0
    n = len(inner)
    color_idx = 0

    def add_clause(raw_start: int, raw_end: int):
        nonlocal color_idx
        chunk = inner[raw_start:raw_end]
        stripped = chunk.strip()
        if stripped:
            lead = len(chunk) - len(chunk.lstrip())
            c_start = base_offset + raw_start + lead
            c_end = c_start + len(stripped)
            clauses.append(ExpressionClause(
                text=stripped,
                start_pos=c_start,
                end_pos=c_end,
                color_index=color_idx % len(SYNCHRONIZED_PALETTE)
            ))
            color_idx += 1

    while i < n:
        c = inner[i]
        if in_quote:
            if c == in_quote:
                in_quote = None
        elif c in ('"', "'"):
            in_quote = c
        elif c in ('(',):
            depth += 1
        elif c in (')',):
            depth = max(0, depth - 1)
        elif c in ('[',):
            bracket_depth += 1
        elif c in (']',):
            bracket_depth = max(0, bracket_depth - 1)
        elif depth == 0 and bracket_depth == 0:
            is_sep = False
            sep_len = 0
            if c == '|':
                rem = inner[i+1:].lstrip()
                if rem and (rem[0].upper() in ('X', 'Y', 'Z') or rem[0] == '('):
                    pass  # Parallel axis operator |X, not a clause separator
                else:
                    is_sep = True
                    sep_len = 1
            elif c in (',', '&'):
                is_sep = True
                sep_len = 1
            else:
                m_word = re.match(r"^(or|and|exc)\b", inner[i:], re.IGNORECASE)
                if m_word:
                    if i == 0 or not (inner[i-1].isalnum() or inner[i-1] == "_"):
                        is_sep = True
                        sep_len = len(m_word.group(1))

            if is_sep:
                add_clause(start, i)
                i += sep_len
                start = i
                continue
        i += 1
    add_clause(start, n)
    return clauses


def compute_synchronized_colors(
    base_obj: Any,
    expr: str,
    kind: Optional[str] = "Face",
    tolerance: float = 1e-4,
    active_clause_idx: Optional[int] = None,
) -> tuple[list[ExpressionClause], dict[str, tuple[float, float, float, float]]]:
    """Compute 1:1 color synchronization between expression clauses and 3D geometry subnames."""
    clauses = decompose_clauses(expr)
    if not clauses or base_obj is None:
        return clauses, {}

    # Evaluate each clause individually to determine which subelements it selects
    for c in clauses:
        try:
            items = evaluate_expression_items(base_obj, c.text, kind=kind, tolerance=tolerance)
            c.matched_subnames = {it.subname for it in items if getattr(it, "subname", None)}
        except Exception:
            c.matched_subnames = set()

    # Also evaluate the entire combined expression
    try:
        all_items = evaluate_expression_items(base_obj, expr, kind=kind, tolerance=tolerance)
        all_subnames = {it.subname for it in all_items if getattr(it, "subname", None)}
    except Exception:
        all_subnames = set().union(*(c.matched_subnames for c in clauses))

    element_colors: dict[str, tuple[float, float, float, float]] = {}

    for sname in all_subnames:
        matching_clauses = [c for c in clauses if sname in c.matched_subnames]
        if len(matching_clauses) == 1:
            c = matching_clauses[0]
            palette = SYNCHRONIZED_PALETTE[c.color_index % len(SYNCHRONIZED_PALETTE)]
            element_colors[sname] = palette["rgba_3d"]
        elif len(matching_clauses) > 1:
            element_colors[sname] = OVERLAP_COLOR["rgba_3d"]
        else:
            element_colors[sname] = OVERLAP_COLOR["rgba_3d"]

    # If an active clause is specified, emphasize its elements
    if active_clause_idx is not None and 0 <= active_clause_idx < len(clauses):
        active_clause = clauses[active_clause_idx]
        active_palette = SYNCHRONIZED_PALETTE[active_clause.color_index % len(SYNCHRONIZED_PALETTE)]
        for sname in active_clause.matched_subnames:
            element_colors[sname] = active_palette["rgba_3d"]

    return clauses, element_colors

