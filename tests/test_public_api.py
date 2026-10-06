import unittest
from dataclasses import dataclass

from feature_selector import selector
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

    def test_robust_selector_module_matches_feature_selector(self):
        import robust_selector
        import feature_selector
        self.assertIs(robust_selector.selector, feature_selector.selector)
        self.assertIs(robust_selector.resolve, feature_selector.resolve)
        self.assertIs(robust_selector.apply, feature_selector.apply)


if __name__ == "__main__":
    unittest.main()
