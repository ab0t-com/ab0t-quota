"""Tests for ab0t_quota.config loaders."""

from ab0t_quota.config import load_resource_bundles


class TestLoadResourceBundles:
    def test_returns_empty_when_missing(self):
        assert load_resource_bundles({}) == {}
        assert load_resource_bundles(None) == {}
        assert load_resource_bundles({"other_key": 1}) == {}

    def test_loads_simple_map(self):
        cfg = {"resource_bundles": {
            "single": ["thing.a"],
            "multi":  ["thing.a", "thing.b"],
        }}
        bundles = load_resource_bundles(cfg)
        assert bundles == {"single": ["thing.a"], "multi": ["thing.a", "thing.b"]}

    def test_skips_invalid_entries_keeps_valid_ones(self):
        cfg = {"resource_bundles": {
            "good":      ["thing.a"],
            "not_list":  "thing.a",       # wrong shape — string instead of list
            "non_str":   ["thing.a", 123], # non-string element
        }}
        bundles = load_resource_bundles(cfg)
        assert bundles == {"good": ["thing.a"]}

    def test_rejects_non_object_root(self):
        cfg = {"resource_bundles": ["this", "should", "be", "a", "dict"]}
        assert load_resource_bundles(cfg) == {}

    def test_returns_independent_lists(self):
        """Mutating the loaded dict shouldn't affect the source config."""
        src = {"resource_bundles": {"x": ["thing.a"]}}
        bundles = load_resource_bundles(src)
        bundles["x"].append("thing.b")
        assert src["resource_bundles"]["x"] == ["thing.a"]
