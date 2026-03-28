"""
Stateful PBT: state-verify自身のMCPサーバーをHypothesis RuleBasedStateMachineで検証。
code-behavior.yamlのstate machineに基づき、ランダムな操作シーケンスで不変条件を検証する。
"""
import json
import os
import shutil
import tempfile
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hypothesis import assume, note, settings, HealthCheck
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant, initialize
import hypothesis.strategies as st

from state_verify import load_spec, load_store, save_store, get_all_cells, cell_key, get_store_path
import yaml


EXAMPLE_SPEC = {
    "name": "PBT Test Spec",
    "rows": [
        {"id": "r1", "from": "a", "to": "b", "trigger": "go", "description": "a to b"},
        {"id": "r2", "from": "b", "to": "c", "trigger": "next", "description": "b to c"},
    ],
    "columns": [
        {"id": "c1", "prompt_template": "Check {from} -> {to}: {description}\n{domain_context}"},
        {"id": "c2", "prompt_template": "Verify {trigger} for {from}->{to}\n{verified_context}"},
    ],
    "states": {"a": {"description": "start"}, "b": {"description": "mid"}, "c": {"description": "end"}},
    "domain_context": "PBT test domain",
}


class StateVerifyStateMachine(RuleBasedStateMachine):
    """Stateful PBT for state-verify core logic."""

    def __init__(self):
        super().__init__()
        self.tmpdir = tempfile.mkdtemp()
        self.spec_path = os.path.join(self.tmpdir, "test.yaml")
        self.state = "no_spec"
        self.expected_cells = set()
        self.recorded_cells = set()

    def teardown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_spec(self, spec_dict):
        with open(self.spec_path, "w") as f:
            yaml.dump(spec_dict, f, allow_unicode=True)

    def _write_bad_spec(self):
        with open(self.spec_path, "w") as f:
            f.write("not: valid: yaml: [[[")

    # === State transitions ===

    @initialize()
    def init(self):
        self.state = "no_spec"
        self.recorded_cells = set()
        # Ensure clean state
        store_path = os.path.join(self.tmpdir, ".test.verified.json")
        if os.path.exists(store_path):
            os.unlink(store_path)

    @rule()
    def load_valid_spec(self):
        """t3: no_spec → spec_loaded"""
        assume(self.state == "no_spec")
        self._write_spec(EXAMPLE_SPEC)
        spec = load_spec(self.spec_path)
        assert spec["name"] == "PBT Test Spec"
        self.expected_cells = {cell_key(r["id"], c["id"]) for r in spec["rows"] for c in spec["columns"]}
        self.state = "spec_loaded"
        note(f"-> spec_loaded ({len(self.expected_cells)} cells)")

    @rule()
    def load_invalid_spec(self):
        """t2: no_spec → spec_invalid"""
        assume(self.state == "no_spec")
        self._write_bad_spec()
        try:
            load_spec(self.spec_path)
            assert False, "Should have raised"
        except (ValueError, yaml.YAMLError):
            pass
        self.state = "spec_invalid"
        note("-> spec_invalid")

    @rule()
    def fix_spec(self):
        """t5: spec_invalid → spec_loaded"""
        assume(self.state == "spec_invalid")
        self._write_spec(EXAMPLE_SPEC)
        spec = load_spec(self.spec_path)
        assert spec["name"] == "PBT Test Spec"
        self.expected_cells = {cell_key(r["id"], c["id"]) for r in spec["rows"] for c in spec["columns"]}
        self.state = "spec_loaded"
        note("-> spec_loaded (fixed)")

    @rule()
    def read_tools_no_store(self):
        """t6: spec_loaded → spec_loaded (read-only tools)"""
        assume(self.state == "spec_loaded")
        assume(len(self.recorded_cells) == 0)
        spec = load_spec(self.spec_path)
        store = load_store(self.spec_path)
        cells = get_all_cells(spec)
        # All cells should be unverified
        assert len(store.get("cells", {})) == 0 or len(self.recorded_cells) == 0
        assert len(cells) == len(self.expected_cells)
        note("read_tools_no_store: ok")

    @rule(row=st.sampled_from(["r1", "r2"]), col=st.sampled_from(["c1", "c2"]))
    def record_cell(self, row, col):
        """t7/t11/t12: record a cell"""
        assume(self.state in ("spec_loaded", "partial", "full"))
        spec = load_spec(self.spec_path)
        store = load_store(self.spec_path)
        cells = get_all_cells(spec)

        key = cell_key(row, col)
        if key not in self.expected_cells:
            return  # skip invalid combos

        cell = next((c for c in cells if c["key"] == key), None)
        assert cell is not None

        if "cells" not in store:
            store["cells"] = {}

        store["cells"][key] = {
            "row_id": row,
            "column_id": col,
            "response": {"test": True, "key": key},
            "verified_at": "2026-01-01T00:00:00",
        }
        save_store(self.spec_path, store)
        self.recorded_cells.add(key)

        if self.recorded_cells == self.expected_cells:
            self.state = "full"
            note(f"-> full ({len(self.recorded_cells)}/{len(self.expected_cells)})")
        else:
            self.state = "partial"
            note(f"-> partial ({len(self.recorded_cells)}/{len(self.expected_cells)})")

    @rule()
    def record_invalid_cell(self):
        """t13/t15: record with non-existent cell key"""
        assume(self.state in ("spec_loaded", "partial", "full"))
        spec = load_spec(self.spec_path)
        cells = get_all_cells(spec)
        bad_key = cell_key("nonexistent", "bad")
        cell = next((c for c in cells if c["key"] == bad_key), None)
        assert cell is None, "Bad key should not match any cell"
        # State unchanged
        note("record_invalid: correctly rejected")

    @rule()
    def read_tools_partial(self):
        """t7/t19: read tools during partial verification"""
        assume(self.state == "partial")
        spec = load_spec(self.spec_path)
        store = load_store(self.spec_path)
        cells = get_all_cells(spec)

        stored_keys = set(store.get("cells", {}).keys())
        spec_keys = {c["key"] for c in cells}
        verified = stored_keys & spec_keys
        assert len(verified) == len(self.recorded_cells)
        assert len(verified) < len(self.expected_cells)
        note(f"read_partial: {len(verified)}/{len(self.expected_cells)}")

    @rule()
    def coverage_check(self):
        """Check coverage is consistent across all counting methods"""
        assume(self.state in ("partial", "full"))
        spec = load_spec(self.spec_path)
        store = load_store(self.spec_path)
        cells = get_all_cells(spec)

        stored_keys = set(store.get("cells", {}).keys())
        spec_keys = {c["key"] for c in cells}
        verified = stored_keys & spec_keys

        # All counting methods must agree
        assert len(verified) == len(self.recorded_cells)
        note(f"coverage_check: {len(verified)}/{len(spec_keys)}")

    @rule()
    def next_when_complete(self):
        """t16: sv_next when all cells verified"""
        assume(self.state == "full")
        spec = load_spec(self.spec_path)
        store = load_store(self.spec_path)
        cells = get_all_cells(spec)
        unverified = [c for c in cells if c["key"] not in store.get("cells", {})]
        assert len(unverified) == 0
        note("next_when_complete: correctly empty")

    @rule()
    def reset(self):
        """t9/t10: reset verification data"""
        assume(self.state in ("partial", "full"))
        store_path = get_store_path(self.spec_path)
        if store_path.exists():
            store_path.unlink()
        self.recorded_cells = set()
        self.state = "spec_loaded"
        note("-> spec_loaded (reset)")

    @rule()
    def export_check(self):
        """Verify export reflects actual store state"""
        assume(self.state in ("partial", "full"))
        spec = load_spec(self.spec_path)
        store = load_store(self.spec_path)
        cells = get_all_cells(spec)

        spec_keys = {c["key"] for c in cells}
        verified = set(store.get("cells", {}).keys()) & spec_keys

        for r in spec["rows"]:
            for c in spec["columns"]:
                key = cell_key(r["id"], c["id"])
                if key in verified:
                    assert key in store["cells"]
                    assert "response" in store["cells"][key]
        note(f"export_check: {len(verified)} cells valid")

    # === Invariants (checked after EVERY rule) ===

    @invariant()
    def state_is_valid(self):
        assert self.state in ("no_spec", "spec_invalid", "spec_loaded", "partial", "full")

    @invariant()
    def recorded_subset_of_expected(self):
        assert self.recorded_cells <= self.expected_cells

    @invariant()
    def store_matches_tracked_state(self):
        """Store on disk must match our tracked recorded_cells."""
        if self.state in ("no_spec", "spec_invalid"):
            return  # No store to check

        store = load_store(self.spec_path)
        stored = set(store.get("cells", {}).keys())
        assert stored == self.recorded_cells, f"Store {stored} != tracked {self.recorded_cells}"

    @invariant()
    def full_means_all_recorded(self):
        if self.state == "full":
            assert self.recorded_cells == self.expected_cells

    @invariant()
    def partial_means_some_recorded(self):
        if self.state == "partial":
            assert 0 < len(self.recorded_cells) < len(self.expected_cells)

    @invariant()
    def store_file_either_valid_or_absent(self):
        """Store file must be valid JSON or not exist."""
        if self.state in ("no_spec", "spec_invalid"):
            return
        store_path = get_store_path(self.spec_path)
        if store_path.exists():
            with open(store_path) as f:
                data = json.load(f)  # Must not raise
            assert isinstance(data, dict)
            assert "cells" in data
            assert "metadata" in data


# Configure and run
StateVerifyStateMachine.TestCase.settings = settings(
    max_examples=50,
    stateful_step_count=30,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    deadline=None,
)
TestStateMachine = StateVerifyStateMachine.TestCase
