"""Suite-wide operator assertion for the EMULATOR (D-71).

The topology guard machine-checks `CLUSTER INFO` at startup and refuses to boot on
an unverifiable topology (unknown fails closed). **fakeredis does not implement the
`CLUSTER` command** — it answers `unknown command 'cluster'` — which is precisely
the "managed Redis that disables CLUSTER INFO" case the guard refuses.

The emulator is, by construction, not a cluster. So the assertion is TRUE, and it is
made ONCE, here, in the open — the same on-the-record assertion a client on a managed
Redis would write into `quota-config.json` (`storage.redis_cluster_confirmed_disabled`).
It is *not* a way to make the guard go away: the guard's own tests
(`test_cluster_topology_guard_d71_20260711.py`) clear this env explicitly and assert
the refusal, and a POSITIVE `cluster_enabled:1` is refused regardless of any assertion.

D-72 adds the second assertion, for the same reason: **fakeredis does not implement
`CONFIG GET` either** (it answers `unknown command 'config|get'`), which is the
"ElastiCache disabled CONFIG" case the counter-eviction guard refuses. fakeredis does
not evict — it is an in-process dict. So the assertion is TRUE, and it is made once,
here, in the open, in exactly the on-the-record form a client on a managed Redis uses
(`storage.redis_durability_confirmed`). The guards' own tests clear both envs and assert
the refusals, and a policy the server actually REPORTS as `allkeys-*` is refused
regardless of any assertion.
"""
import os

os.environ.setdefault("AB0T_QUOTA_REDIS_CLUSTER_CONFIRMED_DISABLED", "true")
os.environ.setdefault("AB0T_QUOTA_REDIS_DURABILITY_CONFIRMED", "true")
