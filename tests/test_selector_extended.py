import unittest
from dataclasses import dataclass

from fs_selector import Step, apply_step


@dataclass(frozen=True)
class V:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class R:
    object_name: str
    subname: str


@dataclass(frozen=True)
class C:
    ref: R
    center: V
    direction: V = V(0, 0, 1)
    geom_type: str = "PLANE"
    length: float = 1.0
    area: float = 1.0
    volume: float = 0.0
    radius: float = 1.0
    perimeter: float = 4.0
    wire_count: int = 1
    vertex_valence: int = 2
    bbox: tuple[float, float, float, float, float, float] = (-1, -1, -1, 1, 1, 1)
    source_bbox: tuple[float, float, float, float, float, float] = (-1, -1, -1, 1, 1, 1)

    @property
    def compactness(self):
        if self.perimeter <= 1e-7 or self.area <= 1e-7:
            return float("nan")
        return 4.0 * 3.141592653589793 * self.area / (self.perimeter * self.perimeter)

    @property
    def bbox_aspect_ratio(self):
        sizes = [abs(self.bbox[3]-self.bbox[0]), abs(self.bbox[4]-self.bbox[1]), abs(self.bbox[5]-self.bbox[2])]
        p = [x for x in sizes if x > 1e-7]
        return max(p) / min(p) if p else 1.0

    @property
    def distance(self):
        return (self.center.x**2 + self.center.y**2 + self.center.z**2) ** 0.5

    def metric(self, name):
        if name in {"x", "y", "z"}:
            return getattr(self.center, name)
        if hasattr(self, name):
            return getattr(self, name)
        raise KeyError(name)


class TestExtendedSelectors(unittest.TestCase):
    def setUp(self):
        self.items = [
            C(R("B", "Face1"), V(0, 0, 0), direction=V(1, 0, 0), wire_count=1),
            C(R("B", "Face2"), V(0, 0, 5), direction=V(0, 0, 1), wire_count=2, bbox=(-1, -1, -1, 2, 1, 1)),
            C(R("B", "Face3"), V(5, 5, 5), direction=V(0, 0, -1), wire_count=1, bbox=(-1, -1, -1, 1, 1, 1)),
        ]

    def test_center_on_axis(self):
        got = apply_step(self.items, Step("filter", {"name": "center_on_axis", "value": {"axis": "Z", "tolerance": 1e-6}}))
        self.assertEqual([c.ref.subname for c in got], ["Face1", "Face2"])

    def test_bbox_touch(self):
        items = [
            C(R("B", "Face1"), V(0, 0, 0), bbox=(-1, -1, -1, 1, 1, 10), source_bbox=(-1, -1, -1, 1, 1, 10)),
            C(R("B", "Face2"), V(0, 0, 0), bbox=(-1, -1, -1, 1, 1, 9), source_bbox=(-1, -1, -1, 1, 1, 10)),
        ]
        got = apply_step(items, Step("filter", {"name": "bbox_touch", "value": {"axis": "Z", "side": "max", "tolerance": 1e-8}}))
        self.assertEqual([c.ref.subname for c in got], ["Face1"])

    def test_facing_and_wire_predicates(self):
        got = apply_step(self.items, Step("filter", {"name": "facing_positive", "value": "X",}))
        self.assertEqual([c.ref.subname for c in got], ["Face1"])
        got = apply_step(self.items, Step("filter", {"name": "has_multiple_wires", "value": True}))
        self.assertEqual([c.ref.subname for c in got], ["Face2"])

    def test_compactness_metric(self):
        items = [
            C(R("B", "Face1"), V(0, 0, 0), area=1.0, perimeter=4.0),
            C(R("B", "Face2"), V(0, 0, 0), area=1.0, perimeter=10.0),
        ]
        got = apply_step(items, Step("extreme", {"metric": "compactness", "direction": "max"}))
        self.assertEqual([c.ref.subname for c in got], ["Face1"])

    def test_bbox_aspect_ratio_metric(self):
        got = apply_step(self.items, Step("extreme", {"metric": "bbox_aspect_ratio", "direction": "max"}))
        self.assertEqual([c.ref.subname for c in got], ["Face2"] )


if __name__ == "__main__":
    unittest.main()
