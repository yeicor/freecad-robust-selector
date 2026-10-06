from fs_selector import Selector, Step


def test_selector_json_roundtrip():
    original = Selector(
        "Face",
        (
            Step("filter", {"name": "geom_type", "value": "PLANE"}, forced=True),
            Step("extreme", {"metric": "z", "direction": "max", "tolerance": 1e-6}),
        ),
        expected_count=1,
        name="top_face",
    )
    restored = Selector.from_json(original.to_json())
    assert restored == original
