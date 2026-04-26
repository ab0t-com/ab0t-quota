# Deployment Runbook — sandbox + billing + payment + ab0t-quota

End-to-end deployment guide for the four-service stack:

| Service | Port (intra-cluster) | Owns |
|---|---|---|
| **payment** | 8005 | Stripe integration, checkout sessions, customer portal, webhooks |
| **billing** | 8002 | Commercial accounts, balance, tier source-of-truth, quota catalog, bridge engine |
| **sandbox-platform** | 8020 | Sandbox / browser / desktop provisioning, consumer of quota+billing+payment |
| **ab0t-quota** (library) | n/a (pip package) | Quota engine + LifecycleEmitter + billing/payment proxy router |

Plus shared infrastructure already running:
- `shared-redis:6379` (quota counters, tier cache, alert cooldowns)
- DynamoDB (`ab0t_quota_state` table — auto-created by the library/billing on first start)
- LocalStack SNS (lifecycle events for billing proration)

---

## Order of operations

```
1. Tag library             →  git tag v0.2.0 in shared/ab0t-quota
2. Build payment service   →  docker compose -f payment/output/docker-compose.yml build
3. Build billing service   →  rebuild AFTER library is tagged; pulls from git tag
4. Build sandbox-platform  →  rebuild AFTER library is tagged
5. Provision mesh creds    →  register sandbox as consumer of billing + payment
6. Configure Stripe        →  webhook URL, price IDs in quota-config.json
7. Boot in sequence        →  shared-infra → payment → billing → sandbox
8. Smoke test              →  hit a few endpoints to confirm wiring
9. Run UJ tests            →  full integration suite
```

---

## Step 1: Tag and publish the library

```bash
cd shared/ab0t-quota
git tag v0.2.0
git push origin v0.2.0
```

Both consumer requirements.txt files pin to `@v0.2.0`. Library is at `0.2.0` in pyproject.toml.

---

## Step 2: Build payment service (no code changes for this release)

```bash
cd payment/output
docker compose build payment
```

Verify Stripe keys are set:
```bash
echo $STRIPE_SECRET_KEY            # sk_live_... or sk_test_...
echo $STRIPE_WEBHOOK_SECRET        # whsec_...
```

---

## Step 3: Build billing service

```bash
cd billing/output
docker compose build billing
```

This rebuild pulls `ab0t-quota @ git+...@v0.2.0` from the requirements.txt. The new code includes:
- `app/modules/quota/engine_factory.py` (per-service bridge engine)
- `app/modules/quota/router.py` — added `PUT /tier-catalog/{service}` and 5 bridge endpoints
- `app/modules/quota/store.py` — catalog storage
- `app/modules/quota/service.py` — catalog publish flow

Verify billing module imports cleanly:
```bash
cd billing/output
./venv/bin/python -c "from app.modules.quota import quota_router; print('OK')"
```

---

## Step 4: Build sandbox-platform

```bash
cd resource/output/sandbox-platform
docker compose build sandbox
```

Rebuild pulls library + applies the Phase 1-4 migration changes already in place:
- `app/quota.py` — slim shim over setup_quota
- `app/events.py` — delegation to library LifecycleEmitter
- `app/main.py` — `quota.bind_app(app)` after FastAPI(); old hand-rolled routes deleted
- No more `app/billing_client.py` or `app/payment_client.py`

---

## Step 5: Provision mesh credentials

Sandbox needs to be registered as a mesh consumer of billing + payment. Run the existing setup script:

```bash
cd resource/output/sandbox-platform/setup
./setup run 07     # register as billing + payment consumer
```

You'll get back two values to put in sandbox's env:

```bash
AB0T_MESH_API_KEY=ab0t_sk_live_<value>
AB0T_CONSUMER_ORG_ID=<sandbox's UUID in the mesh>
```

Save them to `resource/output/sandbox-platform/.env`.

The mesh API key needs the following billing scopes:
- `BillingReader` — for `/billing/{org}/balance`, `/usage`, etc. (proxy reads)
- `BillingAdmin` — for `PUT /billing/tier-catalog/{service}` (catalog publish on startup)

The setup script handles this; verify with:

```bash
curl -H "X-API-Key: $AB0T_MESH_API_KEY" \
     https://billing.service.ab0t.com/billing/$AB0T_CONSUMER_ORG_ID/balance
# expect 200 with balance JSON
```

---

## Step 6: Configure Stripe

### 6a. Stripe price IDs in quota-config.json

Open `resource/output/sandbox-platform/quota-config.json`, find the `billing_integration` block:

```json
{
  "billing_integration": {
    "stripe_price_to_tier": {
      "price_starter_monthly": "starter",
      "price_starter_annual": "starter",
      "price_pro_monthly": "pro",
      "price_pro_annual": "pro",
      "price_enterprise": "enterprise"
    }
  }
}
```

Replace the `price_*` keys with your real Stripe price IDs from the
Stripe dashboard (Products → click product → copy price ID, looks like
`price_1PxYz...`).

### 6b. Stripe webhook URL

In the Stripe dashboard:
- Developers → Webhooks → Add endpoint
- URL: `https://sandbox.your-domain.com/api/webhooks/stripe`
- Events to subscribe:
  - `checkout.session.completed`
  - `customer.subscription.created`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.payment_succeeded`
  - `invoice.payment_failed`
- Copy the signing secret → set `STRIPE_WEBHOOK_SECRET` in payment service env

The library's webhook proxy in sandbox forwards to payment service which
verifies the signature.

---

## Step 7: Boot in sequence

```bash
# 1. Shared infrastructure (must be first)
docker compose -f docker-compose.shared.yml up -d

# 2. Payment service
cd payment/output
docker compose up -d payment

# 3. Billing service
cd billing/output
docker compose up -d billing

# Verify billing is healthy and the quota module loaded:
curl http://localhost:8002/health
docker compose logs billing 2>&1 | grep quota_module_initialized

# 4. Sandbox platform
cd resource/output/sandbox-platform
docker compose up -d sandbox

# Verify sandbox is healthy and the library wired up:
curl http://localhost:8020/api/health
docker compose logs sandbox 2>&1 | grep "quota setup complete"
docker compose logs sandbox 2>&1 | grep "catalog published"
```

Expected log lines on a clean sandbox boot:
```
INFO ab0t_quota.setup: catalog published service=sandbox-platform tiers=4 resources=N bundles=4
INFO ab0t_quota.setup: paid-tier proxy router mounted at prefix=/api
INFO ab0t_quota.setup: quota setup complete: N resources, 4 tiers, 4 bundles, paid=True
```

---

## Step 8: Smoke test

```bash
# Public endpoints (no auth)
curl http://localhost:8020/api/payments/plans
curl http://localhost:8020/api/quotas/tiers

# Catalog publish landed in billing
curl -H "X-API-Key: $AB0T_MESH_API_KEY" \
  "https://billing.service.ab0t.com/billing/$AB0T_CONSUMER_ORG_ID/tier/limits?service=sandbox-platform"
# Should return SANDBOX's actual limits (not library DEFAULT_TIERS)

# Bridge endpoint (proves billing's per-service engine is wired)
curl -X POST -H "X-API-Key: $AB0T_MESH_API_KEY" \
  "https://billing.service.ab0t.com/billing/quota/sandbox-platform/test-org/check/sandbox.concurrent"
# Returns 404 with helpful message because no test-org has tier set yet — that's correct

# Authenticated endpoint via sandbox proxy
curl -H "Authorization: Bearer $USER_JWT" \
  http://localhost:8020/api/billing/balance
```

---

## Step 9: UJ tests

```bash
cd resource/output/sandbox-platform/scripts/curl_tests/user_journeys
bash UJ-050_*.sh    # checkout flow
bash UJ-051_*.sh    # subscription
bash UJ-052_*.sh    # tier upgrade
bash UJ-053_*.sh    # invoice / payment methods

# Billing-side UJ tests
cd billing/output/scripts/curl_tests/user_journeys
bash UJ-030_quota_tier_lifecycle.sh
bash UJ-035_quota_override_lifecycle_with_tier_change.sh
bash UJ-037_quota_payment_webhook_tier_sync.sh
```

If a UJ test fails: check the logs of the failing service for stack traces,
file an issue with the failing UJ output.

---

## Production environment variables

### sandbox-platform/.env (production)

```bash
# Single mesh credential the library uses for all upstreams
AB0T_MESH_API_KEY=ab0t_sk_live_xxx
AB0T_CONSUMER_ORG_ID=00000000-0000-0000-0000-000000000000

# Quota config
QUOTA_CONFIG_PATH=/app/quota-config.json
QUOTA_REDIS_URL=redis://shared-redis:6379/4
QUOTA_STATE_TABLE=ab0t_quota_state

# Cost enforcement (sandbox-specific)
BILLING_ENFORCEMENT_ENABLED=true

# Auth
JWT_SECRET=<your secret>
AUTH_SERVICE_URL=https://auth.service.ab0t.com

# AWS
AWS_REGION=us-east-1
SANDBOX_SUBNET_IDS=subnet-xxx
SANDBOX_SECURITY_GROUP=sg-xxx
```

That's the full required set. **Eight env vars.** Compare to pre-migration:
17+ env vars across BILLING_*, PAYMENT_*, SNS_*, AWS_*, JWT_*, etc.

### billing/.env

```bash
DATABASE_URL=postgresql://...
REDIS_URL=redis://shared-redis:6379/1
CLICKHOUSE_URL=...
QUOTA_STATE_TABLE=ab0t_quota_state
QUOTA_DYNAMODB_ENDPOINT=                # blank in prod (uses default AWS endpoint)
SNS_ALERTS_TOPIC_ARN=arn:aws:sns:...
SQS_LIFECYCLE_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/...
```

### payment/.env

```bash
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
DATABASE_URL=postgresql://...
BILLING_SERVICE_URL=http://billing:8002    # internal-cluster reach
BILLING_SERVICE_API_KEY=<billing's mesh key>
```

(Payment is the ONLY service that still references billing's URL directly — it's a peer-to-peer mesh call from one provider service to another.)

---

## Local development overrides

For local docker-compose with LocalStack, set these in the appropriate `.env.local`:

```bash
# Library-internal overrides — local dev only
AB0T_MESH_BILLING_URL=http://host.docker.internal:8002
AB0T_MESH_PAYMENT_URL=http://host.docker.internal:8005
AB0T_MESH_SNS_LIFECYCLE_TOPIC_ARN=arn:aws:sns:us-east-1:000000000000:resource-lifecycle
AWS_ENDPOINT_URL=http://host.docker.internal:4566   # LocalStack
DYNAMODB_ENDPOINT=http://host.docker.internal:8000  # DynamoDB Local
QUOTA_DYNAMODB_ENDPOINT=http://host.docker.internal:8000
```

These are NOT documented in the consumer-facing API — ops sets them once
for local dev. Production uses library defaults (mesh DNS).

---

## Rollback procedure

If sandbox fails after deploy:

```bash
cd resource/output/sandbox-platform

# Revert to the last known-good library version
sed -i 's/@v0.2.0/@v0.1.0/' requirements.txt

# Rebuild and restart
docker compose build sandbox
docker compose up -d sandbox
```

`v0.1.0` is the pre-migration library — it doesn't have setup_quota.
Sandbox would also need its old `app/quota.py`, `app/events.py`, billing/payment routes restored from git history. So a real rollback is:

```bash
git revert <migration commit range>
docker compose build sandbox
docker compose up -d sandbox
```

Tag the commit BEFORE flipping production:

```bash
git tag pre-quota-migration-2026-04-25
```

---

## Health checks

| Service | Check | Expected |
|---|---|---|
| payment | `curl localhost:8005/health` | `{"status": "healthy"}` |
| billing | `curl localhost:8002/health` | `{"status": "healthy"}` |
| billing quota module | `docker logs billing | grep quota_module_initialized` | `quota_module_initialized table=ab0t_quota_state` |
| sandbox | `curl localhost:8020/api/health` | `{"status": "healthy"}` |
| sandbox quota | `docker logs sandbox | grep "quota setup complete"` | `quota setup complete: N resources, 4 tiers, 4 bundles, paid=True` |
| sandbox catalog publish | `docker logs sandbox | grep "catalog published"` | `catalog published service=sandbox-platform tiers=4 resources=N bundles=4` |
| Redis | `redis-cli -h shared-redis ping` | `PONG` |
| DynamoDB | `aws dynamodb describe-table --table-name ab0t_quota_state` | `TableStatus: ACTIVE` |

---

## Common failures

### "catalog publish failed status=403"

The mesh API key doesn't have `BillingAdmin` scope. Fix: re-run setup script (`setup run 07`) or have the platform admin grant the scope.

### "quota engine not initialized" in sandbox responses

Lifespan startup raised an exception before the engine was published. Check sandbox logs for the actual error — usually Redis unreachable or quota-config.json invalid JSON.

### Stripe webhook 400 "Missing Stripe-Signature"

Webhook URL points at sandbox's old route or the request didn't come from Stripe. Verify the URL in Stripe dashboard matches `https://sandbox.your-domain/api/webhooks/stripe`.

### Frontend pricing page is empty

`GET /api/payments/plans` returned `{"plans": []}`. Either:
- Stripe products not synced into payment service's local cache (run `payment/output/scripts/sync_plans.sh`)
- `consumer_org_id` mismatch between sandbox's env and payment service's product owner

### Tier change doesn't take effect

- Verify Stripe webhook delivered (Stripe dashboard → webhook deliveries)
- Verify payment service's `_sync_subscription_tier` ran (`docker logs payment | grep org_tier_set`)
- Verify billing's tier was actually written (`aws dynamodb get-item --table ab0t_quota_state --key '{"PK":{"S":"ORG#<org>"},"SK":{"S":"TIER"}}'`)
- Verify sandbox's tier cache is fresh (`redis-cli DEL quota:tier:<org>` to force refresh)

---

## After deploy

- Tag the production-good commit: `git tag prod-2026-04-25-quota-v2`
- Update the changelog with the migration summary
- Email mesh ops the new env-var requirements
