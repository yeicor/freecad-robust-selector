import unittest
from dataclasses import dataclass

from robust_selector import selector
from fs_selector import Step


@dataclass(frozen=True)
class V:
    x: float
    y: float
    z: float


class TestPublicApi(unittest.TestCase):
    def test_builder_round_trip(self):
        q = selector(
            "Face",
            [
                Step("filter", {"name": "planar", "value": True}, forced=True),
                Step("extreme", {"metric": "z", "direction": "max"}),
            ],
            expected_count=1,
            name="top_face",
        )
        self.assertEqual(q.to_json(), q.from_json(q.to_json()).to_json())

    def test_robust_selector_exports(self):
        import robust_selector
        self.assertTrue(callable(robust_selector.selector))
        self.assertTrue(callable(robust_selector.resolve))
        self.assertTrue(callable(robust_selector.apply))
        self.assertTrue(callable(robust_selector.create))
        self.assertTrue(callable(robust_selector.bind))


if __name__ == "__main__":
    unittest.main()
