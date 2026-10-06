import unittest
from dataclasses import dataclass

from fs_planner import _candidate_steps


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
    area: float = 1.0
    length: float = 1.0
    volume: float = 1.0
    radius: float = 1.0
    bbox: tuple[float, float, float, float, float, float] = (-1, -1, -1, 1, 1, 10)
    source_bbox: tuple[float, float, float, float, float, float] = (-1, -1, -1, 1, 1, 10)
    wire_count: int = 1
    vertex_valence: int = 2

    @property
    def distance(self):
        return (self.center.x ** 2 + self.center.y ** 2 + self.center.z ** 2) ** 0.5

    def metric(self, name):
        if name in {"x", "y", "z"}:
            return getattr(self.center, name)
        if name == "distance":
            return self.distance
        if name == "perimeter":
            return self.length
        if name in {"bbox_aspect_ratio", "bbox_volume", "bbox_diagonal", "direction_x", "direction_y", "direction_z"}:
            return 1.0
        return getattr(self, name)


class TestPlannerSteps(unittest.TestCase):
    def test_planner_produces_geometric_extreme_for_top_face(self):
        pool = [
        C(Ref("Box", "Face1"), V(0, 0, 0), area=1),
        C(Ref("Box", "Face2"), V(0, 0, 5), area=1),
        C(Ref("Box", "Face3"), V(0, 0, 10), area=1),
    ]
        target = [pool[-1]]
        steps = _candidate_steps(pool, target)
        self.assertTrue(any(
            s.op == "extreme" and s.args["metric"] == "z" and s.args["direction"] == "max"
            for s in steps
        ))


if __name__ == "__main__":
    unittest.main()
