"""CadQuery-style selector expression language for FreeCAD FeatureSelector.

Provides a rich, high-performance expression parser and evaluator following
CadQuery selector semantics (https://cadquery.readthedocs.io/en/latest/selectors.html),
expanded to support component descending and cursor-based autocompletion.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Iterable, Optional, Sequence, Union

from fs_freecad import FeatureRef, shape_type_from_subname


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
#  Evaluator item (lazy wrapper around FreeCAD topological subshapes)
# ---------------------------------------------------------------------------

class EvaluatorItem:
    """Wrapper around a FreeCAD subshape with lazy geometry caching."""

    def __init__(self, shape: Any, subname: str, kind: str, base_obj: Any = None):
        self.shape = shape
        self.subname = subname
        self.kind = kind
        self.base_obj = base_obj
        self._center: Optional[tuple[float, float, float]] = None
        self._normal: Optional[tuple[float, float, float]] = None
        self._tangent: Optional[tuple[float, float, float]] = None
        self._geom_type: Optional[str] = None
        self._area: Optional[float] = None
        self._length_val: Optional[float] = None

    @property
    def center(self) -> tuple[float, float, float]:
        if self._center is None:
            if hasattr(self.shape, "Point"):
                p = self.shape.Point
                self._center = (float(p.x), float(p.y), float(p.z))
            elif hasattr(self.shape, "CenterOfMass"):
                c = self.shape.CenterOfMass
                self._center = (float(c.x), float(c.y), float(c.z))
            else:
                self._center = (0.0, 0.0, 0.0)
        return self._center

    @property
    def geom_type(self) -> str:
        if self._geom_type is None:
            gt = ""
            surf = getattr(self.shape, "Surface", None)
            curv = getattr(self.shape, "Curve", None)
            if surf is not None:
                tid = getattr(surf, "TypeId", "")
                gt = tid.replace("Part::Geom", "").upper()
            elif curv is not None:
                tid = getattr(curv, "TypeId", "")
                gt = tid.replace("Part::Geom", "").upper()
            self._geom_type = gt
        return self._geom_type

    @property
    def normal(self) -> Optional[tuple[float, float, float]]:
        if self._normal is None and self.kind == "Face":
            try:
                surf = getattr(self.shape, "Surface", None)
                if surf and "Plane" in getattr(surf, "TypeId", ""):
                    axis = getattr(surf, "Axis", None)
                    if axis:
                        self._normal = _normalize((float(axis.x), float(axis.y), float(axis.z)))
                if self._normal is None and hasattr(self.shape, "normalAt"):
                    n = self.shape.normalAt(0, 0)
                    self._normal = _normalize((float(n.x), float(n.y), float(n.z)))
            except Exception:
                self._normal = (0.0, 0.0, 1.0)
        return self._normal

    @property
    def tangent(self) -> Optional[tuple[float, float, float]]:
        if self._tangent is None and self.kind == "Edge":
            try:
                curv = getattr(self.shape, "Curve", None)
                if curv and "Line" in getattr(curv, "TypeId", ""):
                    v1 = self.shape.Vertexes[0].Point
                    v2 = self.shape.Vertexes[-1].Point
                    diff = (float(v2.x - v1.x), float(v2.y - v1.y), float(v2.z - v1.z))
                    self._tangent = _normalize(diff)
                elif hasattr(self.shape, "tangentAt"):
                    mid = 0.5 * (self.shape.FirstParameter + self.shape.LastParameter)
                    t = self.shape.tangentAt(mid)
                    self._tangent = _normalize((float(t.x), float(t.y), float(t.z)))
            except Exception:
                self._tangent = (0.0, 0.0, 1.0)
        return self._tangent

    @property
    def area(self) -> float:
        if self._area is None:
            self._area = float(getattr(self.shape, "Area", 0.0) or 0.0)
        return self._area

    @property
    def length_val(self) -> float:
        if self._length_val is None:
            self._length_val = float(getattr(self.shape, "Length", 0.0) or 0.0)
        return self._length_val


# ---------------------------------------------------------------------------
#  CadQuery Selector Filters
# ---------------------------------------------------------------------------

class Filter:
    """Base selector filter."""

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        return list(items)

    def describe(self) -> str:
        return ""


class DirectionMinMaxFilter(Filter):
    """>Axis or <Axis (farthest / minimum in direction vector)."""

    def __init__(self, direction: tuple[float, float, float], is_max: bool = True, tolerance: float = 1e-4):
        self.direction = direction
        self.is_max = is_max
        self.tolerance = tolerance

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        if not items:
            return []
        scores = [_dot(item.center, self.direction) for item in items]
        extreme = max(scores) if self.is_max else min(scores)
        return [item for item, score in zip(items, scores) if abs(score - extreme) <= self.tolerance]

    def describe(self) -> str:
        dir_name = next((k for k, v in STANDARD_AXES.items() if v == self.direction), str(self.direction))
        return f"{'>' if self.is_max else '<'}{dir_name}"


class CenterNthFilter(Filter):
    """>>Axis[N] or <<Axis[N] or >Axis[N] (Nth cluster in direction)."""

    def __init__(self, direction: tuple[float, float, float], n: int, is_max: bool = True, tolerance: float = 1e-4):
        self.direction = direction
        self.n = n
        self.is_max = is_max
        self.tolerance = tolerance

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        if not items:
            return []
        scored = [(item, _dot(item.center, self.direction)) for item in items]
        scored.sort(key=lambda pair: pair[1], reverse=self.is_max)

        clusters: list[list[EvaluatorItem]] = []
        for item, score in scored:
            if not clusters or abs(score - _dot(clusters[-1][0].center, self.direction)) > self.tolerance:
                clusters.append([item])
            else:
                clusters[-1].append(item)

        if not clusters:
            return []
        idx = self.n if self.n >= 0 else len(clusters) + self.n
        if 0 <= idx < len(clusters):
            return clusters[idx]
        return []

    def describe(self) -> str:
        dir_name = next((k for k, v in STANDARD_AXES.items() if v == self.direction), str(self.direction))
        return f"{'>>' if self.is_max else '<<'}{dir_name}[{self.n}]"


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


class TypeFilter(Filter):
    """%Plane, %Cylinder, %Line, %Circle, etc."""

    def __init__(self, geom_type: str):
        self.geom_type = geom_type.strip().upper()

    def apply(self, items: Sequence[EvaluatorItem]) -> list[EvaluatorItem]:
        target = self.geom_type
        return [item for item in items if item.geom_type == target or item.geom_type.endswith(target)]

    def describe(self) -> str:
        return f"%{self.geom_type.capitalize()}"


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
        ("NUMBER", r"-?\d+(?:\.\d+)?"),
        ("AXIS_VEC", r"\([+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?\)"),
        ("OP2", r">>|<<"),
        ("OP1", r"[><|#%+\-]"),
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


def _parse_filter_atom(tokens: list[str], idx: int) -> tuple[Filter, int]:
    """Parse a primary filter atom from tokens[idx:]."""
    if idx >= len(tokens):
        raise ValueError("Unexpected end of expression")

    tok = tokens[idx]

    # Parenthesized subexpression: ( ... )
    if tok == "(":
        inner, next_idx = _parse_filter_or(tokens, idx + 1)
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
        inner_filter, _ = _parse_filter_or(str_tokens, 0)
        return inner_filter, next_idx

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

    # Direction / Extrema / Parallel / Perpendicular operators
    # Cases: >Z, <Z, >>Z, <<Z, |Z, #Z, +Z, -Z
    op = ""
    if tok in (">", "<", ">>", "<<", "|", "#", "+", "-"):
        op = tok
        idx += 1
        if idx >= len(tokens):
            raise ValueError(f"Expected axis or vector after operator {op!r}")
        axis_tok = tokens[idx]
        idx += 1
    else:
        # Check combined token like ">Z" or "|X" or "+Z"
        m = re.match(r"^(>>|<<|[><|#+\-])(.*)$", tok)
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
                raise ValueError(f"Unrecognized selector token: {tok!r}")

    vec = parse_vector(axis_tok)

    # Check for optional [index] e.g. >Z[0] or >>Z[-1]
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
        is_max = op in (">", ">>", "+")
        return CenterNthFilter(vec, index_val, is_max=is_max), idx

    if op == ">":
        return DirectionMinMaxFilter(vec, is_max=True), idx
    if op == "<":
        return DirectionMinMaxFilter(vec, is_max=False), idx
    if op == ">>":
        return CenterNthFilter(vec, 0, is_max=True), idx
    if op == "<<":
        return CenterNthFilter(vec, 0, is_max=False), idx
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


def _parse_filter_not(tokens: list[str], idx: int) -> tuple[Filter, int]:
    """Parse 'not' unary operator."""
    if idx < len(tokens) and tokens[idx].lower() == "not":
        child, next_idx = _parse_filter_not(tokens, idx + 1)
        return NotFilter(child), next_idx
    return _parse_filter_atom(tokens, idx)


def _parse_filter_and(tokens: list[str], idx: int) -> tuple[Filter, int]:
    """Parse 'and' / '&' / '+' intersection."""
    left, idx = _parse_filter_not(tokens, idx)
    while idx < len(tokens):
        op = tokens[idx].lower()
        if op in ("and", "&") or (op == "+" and idx + 1 < len(tokens) and tokens[idx + 1] in (">", "<", "|", "#", "%")):
            right, idx = _parse_filter_not(tokens, idx + 1)
            left = AndFilter(left, right)
        elif op in ("exc", "except"):
            right, idx = _parse_filter_not(tokens, idx + 1)
            left = ExceptFilter(left, right)
        else:
            break
    return left, idx


def _parse_filter_or(tokens: list[str], idx: int) -> tuple[Filter, int]:
    """Parse 'or' / '|' union."""
    left, idx = _parse_filter_and(tokens, idx)
    while idx < len(tokens):
        op = tokens[idx].lower()
        if op == "or" or (op == "|" and idx + 1 < len(tokens) and tokens[idx + 1] not in STANDARD_AXES):
            right, idx = _parse_filter_and(tokens, idx + 1)
            left = OrFilter(left, right)
        else:
            break
    return left, idx


def parse_filter_string(expr: str) -> Filter:
    """Parse a boolean CadQuery filter string into a Filter object."""
    clean = expr.strip()
    if not clean:
        return Filter()
    tokens = _tokenize_filter(clean)
    if not tokens:
        return Filter()
    flt, _ = _parse_filter_or(tokens, 0)
    return flt


def parse_expression(expr_str: str, default_kind: str = "Face") -> ExpressionPipeline:
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
        elif depth == 0 and c == '.' and not (i > 0 and clean[i-1].isdigit() and i + 1 < len(clean) and clean[i+1].isdigit()):
            stages.append("".join(current).strip())
            current = []
        elif depth == 0 and clean[i:i+2] in ("->", ">>"):
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
        if match:
            method_name = match.group(1).lower()
            inner_arg = match.group(2).strip()
            # Strip enclosing quotes if present
            if (inner_arg.startswith('"') and inner_arg.endswith('"')) or (inner_arg.startswith("'") and inner_arg.endswith("'")):
                inner_arg = inner_arg[1:-1].strip()
            kind = KINDS_MAP.get(method_name, method_name.capitalize())
            filter_obj = parse_filter_string(inner_arg) if inner_arg else None
            steps.append(PipelineStep(kind, filter_obj))
        else:
            # Standalone filter expression without method name
            filter_obj = parse_filter_string(stage)
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


def evaluate_expression_items(obj: Any, expr_str: str, kind: Optional[str] = None) -> list[EvaluatorItem]:
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
    pipeline = parse_expression(expr_str, default_kind=default_kind)
    if not pipeline.steps:
        # Empty expression returns all items of default kind
        raw = _extract_subshapes(shape, default_kind)
        return [EvaluatorItem(s, f"{default_kind}{idx+1}", default_kind, obj) for idx, s in enumerate(raw)]

    current_items: list[EvaluatorItem] = []
    for step_idx, step in enumerate(pipeline.steps):
        if step_idx == 0:
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

            # Map child subshapes to base object subname indices via OCC isSame identity
            all_base = _extract_subshapes(shape, step.kind)
            de_duped: list[EvaluatorItem] = []
            seen_indices = set()
            for sub in next_raw:
                matched_idx = next((i for i, b in enumerate(all_base) if b.isSame(sub)), None)
                if matched_idx is not None and matched_idx not in seen_indices:
                    seen_indices.add(matched_idx)
                    de_duped.append(EvaluatorItem(sub, f"{step.kind}{matched_idx+1}", step.kind, obj))
            current_items = de_duped

        if step.filter_obj:
            current_items = step.filter_obj.apply(current_items)

    return current_items


def evaluate_expression(obj: Any, expr_str: str, kind: Optional[str] = None) -> list[FeatureRef]:
    """Evaluate expression against obj and return list of FeatureRef."""
    items = evaluate_expression_items(obj, expr_str, kind)
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
    # Types
    "%Plane", "%Cylinder", "%Line", "%Circle", "%Sphere", "%Cone",
    # Combined filters
    ">Z and %Plane", "<Z and %Plane",
    "|Z and >Y", "|Z and <Y", "|Z and >X", "|Z and <X",
    "|Z and %Cylinder",
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
        pool: list[EvaluatorItem] = []
        all_target_kind_shapes = _extract_subshapes(base_shape, scope_kind)
        seen_indices = set()
        for p in parent_items:
            for s in _extract_subshapes(p.shape, scope_kind):
                idx = next((i for i, b in enumerate(all_target_kind_shapes) if b.isSame(s)), None)
                if idx is not None and idx not in seen_indices:
                    seen_indices.add(idx)
                    pool.append(EvaluatorItem(s, f"{scope_kind}{idx+1}", scope_kind, obj))
    else:
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
#  Interoperability with Legacy Selectors
# ---------------------------------------------------------------------------

def steps_to_expression(steps: Sequence[Any], kind: str) -> str:
    """Convert legacy Steps sequence to CadQuery selector expression."""
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
            if name in ("planar", "cylindrical", "linear", "circular"):
                parts.append(f"%{name.capitalize()}")
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
