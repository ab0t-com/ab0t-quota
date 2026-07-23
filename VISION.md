---
name: mesh-billing-drop-in-vision
description: The overarching goal for the billing/payment/quota system across the ab0t mesh. Any SaaS product can join the mesh and get an instant billing and quota system. Every decision should be measured against this vision. Sandbox-platform is the first consumer proving the system works — one consumer, never the template.
type: project
---

## The Goal

A complete, drop-in billing, payment, and quota system for the ab0t mesh. **Any
SaaS product** joins the mesh, fills out a config file, imports the libraries, and
gets a fully working commercial system out of the box — subscriptions, payments,
invoices, quotas, tier management, and a white-label admin portal to manage it all.

The mesh is the vehicle, not the audience: joining it is how a product gets an
instant commercial system. Nothing in the libraries may assume which product is
calling — see **Key Principle** and **Sandbox-Platform** below.

## How It Should Work for a New Service

1. `./setup run 07` — register as billing + payment consumer
2. Fill out `quota-config.json` with tiers and limits
2a. Provision the declared storage (a Redis meeting `docs/requirements.md`, or
    choose bridge mode) and run `python -m ab0t_quota preflight` in CI — the
    library uses exactly what you declare and refuses to guess
3. `pip install ab0t-quota[billing]` + `create_billing_router()` — 20 backend routes
4. Load the frontend SDK — **delivery undecided** (hosted `<script src="payment.service.ab0t.com/js/…">`
   vs. two vendored files with no external fetch). The built SDK is currently vendored;
   this is the open owner gate T-1 in `tickets/20260722_commercial_ui_layer/`
5. Pricing page, billing page, checkout, portal — all rendered by the SDK
6. White-label admin portal at `payment.service.ab0t.com/admin/` — manage plans, subscriptions, invoices, refunds
7. Done. No custom Stripe code, no custom billing UI, no PCI scope.

## The Three Layers

- **Python library** (`ab0t-quota[billing]`): Backend proxy routes, quota enforcement, tier
  management. **Working and in production use**, with an active hardening programme —
  declared-not-discovered resolution, keyspace v2, `preflight`/`doctor`/`provision`, and the
  removal of consumer-specific defaults from library logic. See `tickets/PROGRAM_BOARD_20260721.md`.
- **JS SDK** (`ab0t-quota-ui`): Frontend components for usage, paywall, plans, invoices, balance
  and subscription. **Built** — four widgets, showcase and hosted pages, white-label by design
  token. Delivery method is the open gate (step 4).
- **Admin portal** (payment service `/admin/`): White-label business management dashboard.
  **Not started.** Checkout and customer-portal backends exist and have no UI yet; the storyboard
  port is scoped but unbegun.

## Key Principle

The payment service IS the platform's "Stripe" — it handles all commercial
operations. The billing service IS the platform's ledger. Together they are
a drop-in commercial layer. Services don't build custom billing code, they
consume the shared system.

## Sandbox-Platform = First Proof

Sandbox-platform is the first mesh service to fully integrate — **the first proof, never
the template.** Its tier names, resource keys and service name are its own config, and none
of them may appear in library logic. Where they did, they were served to other tenants as if
they were theirs; that is the origin of the config-is-king programme. It proves:
- Backend library works (20 routes, 63 UJ tests GREEN)
- Checkout flows work (auth + anonymous, account-first, defense in depth)
- Stripe integration works (real test mode, webhook registered)
- Quota enforcement works (tier-based limits, GPU gating)
- The pattern is repeatable (skill documented, library extractable)

## What Came From Where

The Web Components and admin portal code in `~/random/storyboard/` were built
as a general payment UI system. They happened to be written in the storyboard
project but have nothing to do with storyboard — they're generic payment/billing
components that call the payment (8005) and billing (8002) services. They belong
in the payment service as the admin portal and consumer SDK.

## How to Apply

Every feature decision should ask: "does this make the drop-in easier for the
next service?" If a mesh client needs custom code to use billing/payments, that's
a gap in the shared system. The goal is zero custom code — just config and imports.
