"""ST-EFFECT-1 and ST-WORKING-1 — the Python bindings (bind-or-retire ruling:
BOUND, this file; they were declared 2026-07-12 and never referenced by a
running test until the T-24 census started them RED).

Each test loads the DECLARED item from conformance/scenarios.json and asserts
its machine-checkable payload against the shipped evaluators — the same
data-file-drives-the-assertion shape as the ST-TOPOLOGY-1 binding.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _declared(scenario_id: str) -> dict:
    doc = json.loads((REPO / "conformance" / "scenarios.json").read_text())
    return next(i for i in doc["structural_conformance"] if i["id"] == scenario_id)


class TestSTEffect1PythonBinding:
    """ST-EFFECT-1: check the EFFECT, not just the policy (D-80/D-79)."""

    def test_declared_capability_keys_exist_in_the_judgement(self):
        item = _declared("ST-EFFECT-1")
        assert set(item["capability_keys"]) == {"counter_evictions_observed",
                                                "preflight_reverification"}
        import ab0t_quota.setup as setup_mod
        import inspect
        src = inspect.getsource(setup_mod)
        for key in item["capability_keys"]:
            assert key in src, f"declared capability {key} unknown to setup.py"

    def test_d80_observed_eviction_is_the_fact_and_degrades(self):
        from ab0t_quota.redis_preflight import (
            evaluate_eviction_facts, eviction_facts_ok,
        )
        item = _declared("ST-EFFECT-1")
        assert item["eviction_fact_degrades_health"] is True
        status, detail = evaluate_eviction_facts(5)
        assert status == "evictions_observed"
        assert eviction_facts_ok(f"{status} ({detail})") is False, \
            "an OBSERVED eviction must degrade the health probe"

    def test_d80_zero_is_healthy_and_unreadable_does_not_degrade(self):
        from ab0t_quota.redis_preflight import (
            evaluate_eviction_facts, eviction_facts_ok,
        )
        assert evaluate_eviction_facts(0)[0] == "ok"
        status, _ = evaluate_eviction_facts(None)
        assert status == "unknown" and eviction_facts_ok("unknown") is True, \
            "an unreadable statistic is not itself a hazard (ratified deviation)"

    def test_d79_reverification_required_when_counter_on_redis(self):
        """The declared derivation rule against the shipped derivation code."""
        item = _declared("ST-EFFECT-1")
        assert item["reverification_required_when"] == "the counter store is redis"
        import inspect
        import ab0t_quota.setup as setup_mod
        src = inspect.getsource(setup_mod)
        add = src.find('required.add("preflight_reverification")')
        assert add != -1, "the D-79 derived-requirement is gone from setup.py"


class TestSTWorking1PythonBinding:
    """ST-WORKING-1: a CONFIGURED guarantee is not a WORKING one (D-81/D-82)."""

    def test_d81_status_fields_are_the_declared_facts(self):
        from ab0t_quota.redis_preflight import evaluate_persist_facts, persist_facts_ok
        item = _declared("ST-WORKING-1")
        assert set(item["persist_fact_fields"]) == {
            "aof_last_write_status", "rdb_last_bgsave_status",
            "aof_last_bgrewrite_status"}
        status, detail = evaluate_persist_facts(
            aof_enabled="1", aof_write="err", rdb_bgsave="ok", aof_rewrite="ok")
        assert status == "persist_failing", \
            "a failing AOF write must be NOT-durable however green the config reads"
        assert persist_facts_ok(f"FAILING ({detail})") is False

    def test_d81_unreadable_persistence_is_unknown_not_degraded(self):
        from ab0t_quota.redis_preflight import evaluate_persist_facts, persist_facts_ok
        status, _ = evaluate_persist_facts(aof_enabled=None, aof_write=None,
                                           rdb_bgsave=None, aof_rewrite=None)
        assert status == "unknown" and persist_facts_ok("unknown") is True

    @pytest.mark.asyncio
    async def test_d82_ledger_provisions_with_the_declared_ttl_attribute(self):
        from tests.test_t6_tables_20260721 import FakeDDB
        from ab0t_quota.handler_ledger import DDBLedgerStore
        item = _declared("ST-WORKING-1")
        assert item["ledger_provisions_its_table"] is True

        class TTLFakeDDB(FakeDDB):
            def __init__(self):
                super().__init__()
                self.ttl_updates = []

            async def describe_time_to_live(self, TableName):
                return {"TimeToLiveDescription": {"TimeToLiveStatus": "DISABLED"}}

            async def update_time_to_live(self, **kw):
                self.ttl_updates.append(kw)
                return {}

        fake = TTLFakeDDB()
        await DDBLedgerStore(fake, table_name="t_led").ensure_table()
        assert fake.create_calls, "the ledger must provision its own table (D-82)"
        assert fake.ttl_updates and fake.ttl_updates[0][
            "TimeToLiveSpecification"]["AttributeName"] == item["ledger_ttl_attribute"], \
            "TTL must be enabled on the DECLARED attribute the store writes"
