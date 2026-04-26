---
name: mesh-billing-drop-in-vision
description: The overarching goal for the billing/payment/quota system across the ab0t mesh. Every decision should be measured against this vision. Sandbox-platform is the first consumer proving the system works.
type: project
---

## The Goal

A complete, drop-in billing, payment, and quota system for the ab0t mesh. Any
service joins the mesh, fills out a config file, imports the libraries, and gets
a fully working commercial system out of the box — subscriptions, payments,
invoices, quotas, tier management, and a white-label admin portal to manage it all.

## How It Should Work for a New Service

1. `./setup run 07` — register as billing + payment consumer
2. Fill out `quota-config.json` with tiers and limits
3. `pip install ab0t-quota[billing]` + `create_billing_router()` — 20 backend routes
4. `<script src="payment.service.ab0t.com/js/ab0t-billing.js">` — frontend SDK
5. Pricing page, billing page, checkout, portal — all rendered by the SDK
6. White-label admin portal at `payment.service.ab0t.com/admin/` — manage plans, subscriptions, invoices, refunds
7. Done. No custom Stripe code, no custom billing UI, no PCI scope.

## The Three Layers

- **Python library** (`ab0t-quota[billing]`): Backend proxy routes, quota enforcement, tier management. DONE.
- **JS SDK** (`ab0t-billing.js`): Frontend components for pricing, billing, checkout, usage. TICKET FILED.
- **Admin portal** (payment service `/admin/`): White-label business management dashboard. TICKET FILED.

## Key Principle

The payment service IS the platform's "Stripe" — it handles all commercial
operations. The billing service IS the platform's ledger. Together they are
a drop-in commercial layer. Services don't build custom billing code, they
consume the shared system.

## Sandbox-Platform = First Proof

Sandbox-platform is the first mesh service to fully integrate. It proves:
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
