import unittest
from dataclasses import dataclass
from unittest.mock import patch

from fs_planner import plan_selectors
from fs_selector import Step


@dataclass(frozen=True)
class V:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Ref:
    object_name: str
    subname: str


@dataclass(frozen=True)
class C:
    ref: Ref
    center: V
    geom_type: str = "PLANE"
    direction: V = V(0, 0, 1)
    area: float = 1
    length: float = 1
    volume: float = 1
    radius: float = 1

    @property
    def distance(self):
        return (self.center.x**2 + self.center.y**2 + self.center.z**2) ** 0.5

    def metric(self, name):
        if name in {"x", "y", "z"}:
            return getattr(self.center, name)
        if name == "distance":
            return self.distance
        return getattr(self, name)


@dataclass(frozen=True)
class Obj:
    Name: str = "Box"


class TestPlannerRequired(unittest.TestCase):
    def test_forced_step_is_preserved_at_its_stage(self):
        pool = [
            C(Ref("Box", "Face1"), V(0, 0, 0), geom_type="PLANE"),
            C(Ref("Box", "Face2"), V(0, 0, 5), geom_type="CYLINDER"),
            C(Ref("Box", "Face3"), V(0, 0, 10), geom_type="PLANE"),
        ]
        required = [Step("filter", {"name": "geom_type", "value": "PLANE"}, forced=True)]
        with patch("fs_planner.candidates", return_value=pool):
            plans = plan_selectors(Obj(), "Face", ["Face3"], max_depth=2, required_steps=required)
        self.assertTrue(plans)
        self.assertTrue(plans[0].selector.steps[0].forced)
        self.assertEqual(plans[0].selector.steps[0].args["value"], "PLANE")

    def test_required_positions_keep_multiple_forced_steps_at_exact_positions(self):
        pool = [
            C(Ref("Box", "Face1"), V(0, 0, 0), geom_type="PLANE"),
            C(Ref("Box", "Face2"), V(0, 0, 5), geom_type="CYLINDER"),
            C(Ref("Box", "Face3"), V(0, 0, 10), geom_type="PLANE"),
        ]
        first = Step("filter", {"name": "geom_type", "value": "PLANE"}, forced=True)
        second = Step("extreme", {"metric": "z", "direction": "max", "tolerance": 1e-6}, forced=True)
        with patch("fs_planner.candidates", return_value=pool):
            plans = plan_selectors(Obj(), "Face", ["Face3"], max_depth=2, required_positions={0: first, 1: second})
        self.assertTrue(plans)
        self.assertTrue(all(step.forced for step in plans[0].selector.steps))
        self.assertEqual(plans[0].selector.steps[0].args["name"], "geom_type")
        self.assertEqual(plans[0].selector.steps[1].args["metric"], "z")

    def test_completion_preserves_downstream_forced_row_position(self):
        pool = [
            C(Ref("Box", "Face1"), V(0, 0, 0), geom_type="PLANE"),
            C(Ref("Box", "Face2"), V(0, 0, 5), geom_type="CYLINDER"),
            C(Ref("Box", "Face3"), V(0, 0, 10), geom_type="PLANE"),
        ]
        prefix_step = Step("filter", {"name": "geom_type", "value": "PLANE"}, forced=True)
        downstream = Step("extreme", {"metric": "z", "direction": "max", "tolerance": 1e-6}, forced=True)
        with patch("fs_planner.candidates", return_value=pool):
            from fs_planner import complete_selector
            plans = complete_selector(Obj(), "Face", ["Face3"], prefix_steps=(prefix_step,), forced_positions={1: downstream}, max_added_depth=2)
        self.assertTrue(plans)
        steps = plans[0].selector.steps
        self.assertEqual(len(steps), 2)
        self.assertTrue(steps[1].forced)
        self.assertEqual(steps[1].args["metric"], "z")

if __name__ == "__main__":
    unittest.main()
