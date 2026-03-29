# Frontend Integration for Paid Tiers

## Pricing Page

Load plans dynamically, render upgrade buttons:

```javascript
// Load plans from backend
const plans = await SandboxAPI.payments.getPlans();

// Render plan cards with upgrade buttons
plans.forEach(plan => {
    if (plan.tier_id === 'free') {
        renderButton('Start Free', () => window.location = '/dashboard');
    } else if (plan.tier_id === 'enterprise') {
        renderButton('Contact Sales', () => window.location = 'mailto:sales@ab0t.com');
    } else {
        renderButton(`Upgrade to ${plan.display_name}`, async () => {
            const { checkout_url } = await SandboxAPI.payments.createCheckout(plan.plan_id);
            window.location = checkout_url;  // Redirect to Stripe
        });
    }
});
```

## Dashboard Usage Bars

```javascript
// Load usage from quota API
const usage = await fetch('/api/quotas/usage', { headers: authHeaders });
const data = await usage.json();

// Render per-resource usage bars
data.resources.forEach(resource => {
    const pct = resource.utilization ? Math.round(resource.utilization * 100) : 0;
    const color = resource.severity === 'warning' ? 'yellow'
                : resource.severity === 'critical' ? 'red'
                : 'green';
    renderUsageBar(resource.display_name, resource.current, resource.limit, pct, color);
});

// Show tier badge
renderTierBadge(data.tier_id, data.tier_display);
```

## Feature Gating

```javascript
// Load tier features
const tiers = await fetch('/api/quotas/tiers');
const currentTier = tiers.tiers.find(t => t.tier_id === userTier);

// Grey out features not in current tier
if (!currentTier.features.includes('gpu_access')) {
    gpuButton.disabled = true;
    gpuButton.title = 'Available on Pro plan';
    gpuButton.classList.add('tier-locked');
}
```

## Success/Cancel Pages

After Stripe checkout redirects back:

**Success:** `/billing/checkout/success?session_id={id}`
- Show: "Welcome to {tier_name}! Your limits have been upgraded."
- Load `/api/quotas/usage` to show new limits
- Link to dashboard

**Cancel:** `/billing/checkout/cancel`
- Show: "Checkout cancelled. You can upgrade anytime."
- Link back to pricing

## API Endpoints Used

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET /api/payments/plans` | List available plans + prices | Reader |
| `POST /api/payments/checkout/{plan_id}` | Create Stripe checkout → returns checkout_url | Writer |
| `GET /api/quotas/usage` | Current org usage (all resources) | Reader |
| `GET /api/quotas/tiers` | All tier definitions (for pricing page) | Public |
| `GET /api/quotas/check/{resource_key}` | Pre-flight: can I create one more? | Reader |
| `GET /api/payments/subscriptions` | Current subscriptions | Reader |
| `DELETE /api/payments/subscriptions/{id}` | Cancel subscription | Writer |
