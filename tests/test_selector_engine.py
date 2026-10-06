import unittest
from dataclasses import dataclass

from fs_selector import Step, Selector, apply_step


@dataclass(frozen=True)
class V:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class FakeRef:
    object_name: str
    subname: str


@dataclass(frozen=True)
class C:
    ref: object
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


class TestSelectorSteps(unittest.TestCase):
    def setUp(self):
        self.items = [
            C(FakeRef("Box", "Face1"), V(0, 0, 0), area=10),
            C(FakeRef("Box", "Face2"), V(0, 0, 5), area=20),
            C(FakeRef("Box", "Face3"), V(0, 0, 10), area=30),
            C(FakeRef("Box", "Face4"), V(0, 0, 7), geom_type="CYLINDER", area=5),
        ]

    def test_furthest_z(self):
        got = apply_step(self.items, Step("extreme", {"metric":"z", "direction":"max"}))
        self.assertEqual([c.ref.subname for c in got], ["Face3"])

    def test_largest_area(self):
        got = apply_step(self.items, Step("extreme", {"metric":"area", "direction":"max"}))
        self.assertEqual([c.ref.subname for c in got], ["Face3"])

    def test_combined_filter_then_extreme(self):
        planar = apply_step(self.items, Step("filter", {"name":"geom_type", "value":"PLANE"}))
        got = apply_step(planar, Step("extreme", {"metric":"area", "direction":"max"}))
        self.assertEqual([c.ref.subname for c in got], ["Face3"])

    def test_extreme_keeps_ties(self):
        items = self.items + [C(FakeRef("Box", "Face5"), V(0, 0, 10), area=25)]
        got = apply_step(items, Step("extreme", {"metric":"z", "direction":"max"}))
        self.assertEqual({c.ref.subname for c in got}, {"Face3", "Face5"})

    def test_metric_equal(self):
        got = apply_step(self.items, Step("filter", {
            "name":"metric_equal",
            "value":{"metric":"area", "value":20.0, "tolerance":1e-8},
        }))
        self.assertEqual([c.ref.subname for c in got], ["Face2"])

    def test_label_examples(self):
        self.assertEqual(Step("extreme", {"metric": "z", "direction": "max"}).label(), "Furthest Z+")
        self.assertEqual(Step("extreme", {"metric": "length", "direction": "max"}).label(), "Longest")
        self.assertEqual(Step("extreme", {"metric": "area", "direction": "max"}).label(), "Most area")

    def test_sort_take(self):
        got = apply_step(self.items, Step("sort_take", {
            "metric":"z", "direction":"max", "count":2,
        }))
        self.assertEqual([c.ref.subname for c in got], ["Face3", "Face4"])
