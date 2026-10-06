"""Bounded semantic-selector planner.

The planner searches *ways of describing the user's current selection* using
geometric/topological operations. Native topology names are used only as the
comparison key for the current shape; they are never emitted as selector intent.

Plans are ordered first by selector count, then by an explicit stability/readability
cost, then by the number of forced design-intent steps. The search is deliberately
bounded so the interactive editor stays predictable on large shapes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

from fs_selector import AXES, Candidate, METRICS, Selector, Step, apply_step, candidates


@dataclass(frozen=True)
class Plan:
    selector: Selector
    score: tuple[int, int, int]
    explanation: str


# Lower is preferred after the primary criterion (fewest selector operations).
STABILITY = {
    "geom_type": 0,
    "planar": 0,
    "curved": 0,
    "linear": 0,
    "circular": 0,
    "elliptical": 0,
    "spherical": 0,
    "cylindrical": 0,
    "conical": 0,
    "toroidal": 0,
    "closed": 1,
    "valid": 1,
    "boundary": 1,
    "manifold_edge": 1,
    "non_manifold_edge": 2,
    "has_holes": 1,
    "has_single_wire": 1,
    "has_multiple_wires": 1,
    "isolated_vertex": 1,
    "endpoint_vertex": 1,
    "axis_parallel": 0,
    "axis_perpendicular": 0,
    "axis_same_direction": 2,
    "axis_opposite_direction": 2,
    "positive": 1,
    "negative": 1,
    "facing_positive": 1,
    "facing_negative": 1,
    "angle_to_axis": 2,
    "bbox_contains_origin": 2,
    "bbox_touch": 1,
    "center_on_axis": 1,
    "convexity": 0,
    "metric_range": 2,
    "coplanar_with": 1,
    "coaxial_with": 1,
    "metric_equal": 2,
    "metric_compare": 3,
    "count_compare": 2,
    "sort_take": 8,
    "take": 7,
    "skip": 7,
}


def _key(candidate: Candidate) -> tuple[str, str]:
    ref = candidate.ref
    return str(getattr(ref, "object_name", "")), str(getattr(ref, "subname", ""))


def _state_key(items: Iterable[Candidate]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(_key(c) for c in items))


def _same_target(current: Iterable[Candidate], target_keys: set[tuple[str, str]]) -> bool:
    return set(_state_key(current)) == target_keys


def _contains_target(current: Iterable[Candidate], target_keys: set[tuple[str, str]]) -> bool:
    return target_keys.issubset(set(_state_key(current)))


def _freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _step_key(step: Step):
    return step.op, _freeze(step.args)


def _dedupe(steps: Iterable[Step]) -> list[Step]:
    seen = set()
    out = []
    for step in steps:
        key = _step_key(step)
        if key in seen:
            continue
        seen.add(key)
        out.append(step)
    return out


def _metric(candidate: Candidate, name: str) -> float:
    try:
        value = candidate.metric(name)
    except Exception:
        value = getattr(candidate, name, float("nan"))
    try:
        return float(value)
    except Exception:
        return float("nan")


def _predicate_bool(candidate: Candidate, name: str) -> Optional[bool]:
    geom = str(getattr(candidate, "geom_type", ""))
    if name == "planar":
        return geom == "PLANE"
    if name == "curved":
        return geom not in {"PLANE", "LINE"}
    if name == "linear":
        return geom == "LINE"
    if name == "circular":
        return geom == "CIRCLE"
    if name == "elliptical":
        return geom == "ELLIPSE"
    if name == "spherical":
        return geom == "SPHERE"
    if name == "cylindrical":
        return geom == "CYLINDER"
    if name == "conical":
        return geom == "CONE"
    if name == "toroidal":
        return geom == "TORUS"
    if name in {"closed", "valid"}:
        return getattr(candidate, name, None)
    if name == "has_holes":
        return int(getattr(candidate, "hole_count", 0) or 0) > 0
    if name == "has_single_wire":
        return int(getattr(candidate, "wire_count", 0) or 0) == 1
    if name == "has_multiple_wires":
        return int(getattr(candidate, "wire_count", 0) or 0) > 1
    if name == "isolated_vertex":
        return int(getattr(candidate, "vertex_valence", 0) or 0) == 0
    if name == "endpoint_vertex":
        return int(getattr(candidate, "vertex_valence", 0) or 0) == 1
    if name == "boundary":
        return int(getattr(candidate, "adjacent_face_count", 0) or 0) == 1
    if name == "manifold_edge":
        return int(getattr(candidate, "adjacent_face_count", 0) or 0) == 2
    if name == "non_manifold_edge":
        return int(getattr(candidate, "adjacent_face_count", 0) or 0) > 2
    if name == "bbox_contains_origin":
        bbox = getattr(candidate, "bbox", (0.0,) * 6)
        try:
            return (
                bbox[0] <= 0 <= bbox[3]
                and bbox[1] <= 0 <= bbox[4]
                and bbox[2] <= 0 <= bbox[5]
            )
        except Exception:
            return None
    return None


def _direction_relation(candidate: Candidate, axis: str, name: str, tolerance: float = 1e-6) -> bool:
    direction = getattr(candidate, "direction", None)
    if direction is None:
        return False
    ax = AXES[axis]
    try:
        dot = direction.x * ax[0] + direction.y * ax[1] + direction.z * ax[2]
    except Exception:
        return False
    if name == "axis_parallel":
        return abs(abs(dot) - 1.0) <= tolerance
    if name == "axis_perpendicular":
        return abs(dot) <= tolerance
    if name == "axis_same_direction":
        return dot >= 1.0 - tolerance
    if name == "axis_opposite_direction":
        return dot <= -1.0 + tolerance
    return False


def _step_cost(step: Step) -> int:
    if step.op == "filter":
        return STABILITY.get(str(step.args.get("name", "filter")), 5)
    if step.op == "extreme":
        return 0
    return STABILITY.get(step.op, 5)


def _plan_score(steps: Iterable[Step]) -> tuple[int, int, int]:
    chain = tuple(steps)
    return len(chain), sum(_step_cost(step) for step in chain), sum(bool(step.forced) for step in chain)


def _valid_shrinking_step(items: list[Candidate], target: list[Candidate], step: Step) -> bool:
    try:
        trial = apply_step(items, step)
    except Exception:
        return False
    return bool(trial) and len(trial) < len(items) and _contains_target(trial, {_key(c) for c in target})


def _bool_filter_candidates(items: list[Candidate], target: list[Candidate], name: str, value: bool) -> Optional[Step]:
    if not all(_predicate_bool(c, name) is value for c in target):
        return None
    step = Step("filter", {"name": name, "value": value})
    return step if _valid_shrinking_step(items, target, step) else None


def _separating_compare(items: list[Candidate], target: list[Candidate], metric: str) -> list[Step]:
    target_keys = {_key(c) for c in target}
    target_values = [_metric(c, metric) for c in target]
    non_target_values = [_metric(c, metric) for c in items if _key(c) not in target_keys]
    all_values = target_values + non_target_values
    if not target_values or any(not math.isfinite(value) for value in all_values):
        return []
    tmin, tmax = min(target_values), max(target_values)
    tol = 1e-7 * max(1.0, abs(tmin), abs(tmax))
    steps = []
    below = [value for value in non_target_values if value < tmin - tol]
    if below and max(below) < tmin - tol:
        steps.append(Step("filter", {"name": "metric_compare", "value": {
            "metric": metric, "operator": ">=", "value": (max(below) + tmin) / 2.0, "tolerance": tol,
        }}))
    above = [value for value in non_target_values if value > tmax + tol]
    if above and min(above) > tmax + tol:
        steps.append(Step("filter", {"name": "metric_compare", "value": {
            "metric": metric, "operator": "<=", "value": (min(above) + tmax) / 2.0, "tolerance": tol,
        }}))
    return [step for step in steps if _valid_shrinking_step(items, target, step)]


def _angle_candidate(items: list[Candidate], target: list[Candidate], axis: str) -> Optional[Step]:
    axis_vector = AXES[axis]
    values = []
    for candidate in target:
        direction = getattr(candidate, "direction", None)
        if direction is None:
            return None
        try:
            dot = direction.x * axis_vector[0] + direction.y * axis_vector[1] + direction.z * axis_vector[2]
        except Exception:
            return None
        if not math.isfinite(dot):
            return None
        values.append(math.degrees(math.acos(max(-1.0, min(1.0, abs(dot))))))
    if not values or max(values) - min(values) > 1e-6:
        return None
    step = Step("filter", {"name": "angle_to_axis", "value": {
        "axis": axis, "value": sum(values) / len(values), "tolerance": 1e-5,
    }})
    return step if _valid_shrinking_step(items, target, step) else None


def _candidate_steps(items: list[Candidate], target: list[Candidate], max_take: int = 6) -> list[Step]:
    """Generate useful one-step reducers that retain every target candidate."""
    if not items or not target:
        return []
    target_keys = {_key(c) for c in target}
    steps: list[Step] = []

    target_types = {str(getattr(c, "geom_type", "")) for c in target}
    if len(target_types) == 1 and next(iter(target_types), ""):
        step = Step("filter", {"name": "geom_type", "value": next(iter(target_types))})
        if _valid_shrinking_step(items, target, step):
            steps.append(step)

    for name in (
        "planar", "curved", "linear", "circular", "elliptical", "spherical", "cylindrical",
        "conical", "toroidal", "closed", "valid", "boundary", "manifold_edge",
        "non_manifold_edge", "has_holes", "has_single_wire", "has_multiple_wires",
        "isolated_vertex", "endpoint_vertex", "bbox_contains_origin",
    ):
        values = [_predicate_bool(c, name) for c in target]
        if values and all(value is not None and value == values[0] for value in values):
            step = _bool_filter_candidates(items, target, name, bool(values[0]))
            if step:
                steps.append(step)

    for axis in AXES:
        for side in ("min", "max"):
            tol = 1e-6 * max(1.0, max((abs(v) for c in items for v in getattr(c, "source_bbox", (0.0,) * 6)), default=0.0))
            index = {"X": (0, 3), "Y": (1, 4), "Z": (2, 5)}[axis][1 if side == "max" else 0]
            if all(abs(getattr(c, "bbox", (0.0,) * 6)[index] - getattr(c, "source_bbox", (0.0,) * 6)[index]) <= tol for c in target):
                step = Step("filter", {"name": "bbox_touch", "value": {"axis": axis, "side": side, "tolerance": tol}})
                if _valid_shrinking_step(items, target, step):
                    steps.append(step)
        orthogonal = {"X": ("y", "z"), "Y": ("x", "z"), "Z": ("x", "y")}[axis]
        if all(abs(float(getattr(c.center, orthogonal[0]))) <= 1e-6 and abs(float(getattr(c.center, orthogonal[1]))) <= 1e-6 for c in target):
            step = Step("filter", {"name": "center_on_axis", "value": {"axis": axis, "tolerance": 1e-6}})
            if _valid_shrinking_step(items, target, step):
                steps.append(step)
        for facing_name in ("facing_positive", "facing_negative"):
            directions = [getattr(c, "direction", None) for c in target]
            if all(d is not None for d in directions):
                sign = 1.0 if facing_name == "facing_positive" else -1.0
                ax = AXES[axis]
                if all(sign * (d.x * ax[0] + d.y * ax[1] + d.z * ax[2]) >= 1.0 - 1e-6 for d in directions):
                    step = Step("filter", {"name": facing_name, "value": axis})
                    if _valid_shrinking_step(items, target, step):
                        steps.append(step)
        for name in ("axis_parallel", "axis_perpendicular", "axis_same_direction", "axis_opposite_direction"):
            if all(_direction_relation(c, axis, name) for c in target):
                step = Step("filter", {"name": name, "value": axis})
                if _valid_shrinking_step(items, target, step):
                    steps.append(step)
        angle_step = _angle_candidate(items, target, axis)
        if angle_step:
            steps.append(angle_step)
        for sign_name in ("positive", "negative"):
            coordinate = {"X": "x", "Y": "y", "Z": "z"}[axis]
            centers = []
            for candidate in target:
                center = getattr(candidate, "center", None)
                if center is None:
                    centers = []
                    break
                centers.append(getattr(center, coordinate, float("nan")))
            if centers and all(math.isfinite(float(v)) for v in centers):
                if all((float(v) >= -1e-6) if sign_name == "positive" else (float(v) <= 1e-6) for v in centers):
                    step = Step("filter", {"name": sign_name, "value": axis})
                    if _valid_shrinking_step(items, target, step):
                        steps.append(step)

    for metric in METRICS:
        values = [_metric(c, metric) for c in target]
        if not values or any(not math.isfinite(value) for value in values):
            continue
        tol = 1e-6 * max(1.0, max(abs(value) for value in values))
        if max(values) - min(values) <= tol:
            predicate_name = "count_compare" if metric.endswith("_count") or metric in {"hole_count", "adjacent_face_count", "vertex_valence"} else "metric_equal"
            step = Step("filter", {"name": predicate_name, "value": {
                "metric": metric, "operator": "==", "value": sum(values) / len(values), "tolerance": tol,
            }})
            if _valid_shrinking_step(items, target, step):
                steps.append(step)
        steps.extend(_separating_compare(items, target, metric))
        for direction in ("min", "max"):
            step = Step("extreme", {"metric": metric, "direction": direction, "tolerance": tol})
            if _valid_shrinking_step(items, target, step):
                steps.append(step)

    # Rank-based selectors remain a deliberate last resort.
    if len(target) <= max_take:
        for metric in METRICS:
            values = [_metric(candidate, metric) for candidate in items]
            if any(not math.isfinite(value) for value in values):
                continue
            for direction in ("min", "max"):
                ordered = sorted(items, key=lambda c: _metric(c, metric), reverse=direction == "max")
                for count in range(1, min(max_take, len(ordered)) + 1):
                    if {_key(c) for c in ordered[:count]} == target_keys:
                        steps.append(Step("sort_take", {"metric": metric, "direction": direction, "count": count}))
                        break
    return _dedupe(steps)


def _forced_step_valid(state: list[Candidate], target_keys: set[tuple[str, str]], step: Step) -> Optional[list[Candidate]]:
    try:
        trial = apply_step(state, step)
    except Exception:
        return None
    if not trial or not _contains_target(trial, target_keys):
        return None
    return trial


def _infer_kind(pool: list[Candidate], default: str = "Shape") -> str:
    if not pool:
        return default
    ref = pool[0].ref
    return str(getattr(ref, "kind", default) or default)


def _search(
    pool: list[Candidate],
    target_keys: set[tuple[str, str]],
    *,
    kind: str = "Shape",
    prefix: tuple[Step, ...] = (),
    forced_positions: Optional[dict[int, Step]] = None,
    max_depth: int = 6,
    max_results: int = 30,
    max_variants_per_state: int = 5,
) -> list[Plan]:
    """Bounded breadth-first search with exact position preservation."""
    state = list(pool)
    for step in prefix:
        try:
            state = apply_step(state, step)
        except Exception:
            return []
        if not state or not _contains_target(state, target_keys):
            return []

    if forced_positions:
        forced_positions = {int(pos): step for pos, step in forced_positions.items() if int(pos) >= len(prefix)}
        if any(pos >= max_depth for pos in forced_positions):
            return []

    found: dict[str, Plan] = {}
    frontier: list[tuple[list[Step], list[Candidate]]] = [(list(prefix), state)]
    seen: dict[tuple[tuple[str, str], ...], list[tuple]] = {}
    min_solution_depth: Optional[int] = None

    while frontier:
        next_frontier: list[tuple[list[Step], list[Candidate]]] = []
        for chain, current in frontier:
            exact = _same_target(current, target_keys)
            positions_passed = not forced_positions or all(len(chain) > pos for pos in forced_positions)
            if exact and positions_passed:
                selector = Selector(kind, tuple(chain), len(target_keys), "planned")
                found.setdefault(selector.to_json(), Plan(selector, _plan_score(chain), selector.describe()))
                if min_solution_depth is None or len(chain) < min_solution_depth:
                    min_solution_depth = len(chain)
                continue

            if min_solution_depth is not None and len(chain) >= min_solution_depth + 1 and len(found) >= max_results:
                continue
            if len(chain) >= max_depth:
                continue

            forced_at_position = forced_positions.get(len(chain)) if forced_positions else None
            if forced_at_position is not None:
                auto_steps = [Step(forced_at_position.op, dict(forced_at_position.args), forced=True)]
            else:
                auto_steps = _candidate_steps(current, [c for c in current if _key(c) in target_keys])
                auto_steps.sort(key=_step_cost)
                auto_steps = auto_steps[:35]

            is_forced = forced_at_position is not None
            for step in auto_steps:
                try:
                    trial = apply_step(current, step)
                except (KeyError, ValueError, TypeError, ZeroDivisionError):
                    continue
                if not trial or not _contains_target(trial, target_keys):
                    continue
                if not is_forced and len(trial) >= len(current):
                    continue
                state_key = _state_key(trial)
                chain_key = tuple(_step_key(item) + (bool(item.forced),) for item in chain + [step])
                variants = seen.setdefault(state_key, [])
                if chain_key in variants or len(variants) >= max_variants_per_state:
                    continue
                variants.append(chain_key)
                next_frontier.append((chain + [step], trial))
        frontier = next_frontier

    return sorted(found.values(), key=lambda plan: plan.score)[:max_results]


def _pool_and_target_keys(
    obj: Any,
    kind: str,
    target_subnames: Iterable[str],
) -> tuple[list[Candidate], set[tuple[str, str]]]:
    pool = obj if isinstance(obj, list) else candidates(obj, kind)
    source_name = (obj[0].ref.object_name if obj else "") if isinstance(obj, list) else getattr(obj, "Name", "")
    target_keys = {(source_name, str(name)) for name in target_subnames}
    return pool, target_keys


def plan_selectors(
    obj: Any,
    kind: str,
    target_subnames: Iterable[str],
    max_depth: int = 6,
    max_results: int = 30,
    required_steps: Iterable[Step] = (),
    required_positions: Optional[dict[int, Step]] = None,
) -> list[Plan]:
    pool, target_keys = _pool_and_target_keys(obj, kind, target_subnames)
    if not any(_key(candidate) in target_keys for candidate in pool):
        return []
    if _same_target(pool, target_keys):
        selector = Selector(kind, (), len(target_keys), "planned")
        return [Plan(selector, _plan_score(()), selector.describe())]
    positions = required_positions
    if positions is None:
        required = tuple(step for step in required_steps if step.forced)
        if required:
            positions = {index: step for index, step in enumerate(required)}
    return _search(
        pool,
        target_keys,
        kind=kind,
        forced_positions=positions,
        max_depth=max_depth,
        max_results=max_results,
    )


def complete_selector(
    obj: Any,
    kind: str,
    target_subnames: Iterable[str],
    prefix_steps: Iterable[Step] = (),
    forced_positions: Optional[dict[int, Step]] = None,
    max_added_depth: int = 6,
    max_results: int = 12,
) -> list[Plan]:
    """Replan lower rows after an edit while retaining the edited prefix verbatim."""
    pool, target_keys = _pool_and_target_keys(obj, kind, target_subnames)
    if not any(_key(c) in target_keys for c in pool):
        return []
    prefix = tuple(prefix_steps)
    positions = dict(forced_positions or {})
    if not prefix and _same_target(pool, target_keys) and not positions:
        selector = Selector(kind, (), len(target_keys), "planned")
        return [Plan(selector, _plan_score(()), selector.describe())]
    return _search(
        pool,
        target_keys,
        kind=kind,
        prefix=prefix,
        forced_positions=positions,
        max_depth=max(len(prefix) + max_added_depth, max(positions, default=-1) + 1),
        max_results=max_results,
    )
