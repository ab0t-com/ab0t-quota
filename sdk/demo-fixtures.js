/*
 * demo-fixtures.js — the shared fake back-end for every page in sdk/.
 *
 * NOT PART OF THE SDK. This file exists so demo.html, showcase.html and the
 * three hosted-page compositions all speak to the SAME payload shapes without
 * copy-pasting them five times. It ships no behaviour the SDK relies on.
 *
 * Every object below is the literal wire shape of a real endpoint:
 *   usage    GET /api/quotas/usage             engine.py:1155-1182
 *   tiers    GET /api/quotas/tiers             setup.py:2361-2380
 *   denial   429 body                          models/responses.py:103-117
 *   invoices GET /api/payments/invoices        billing/models.py:251-270
 *   balance  GET /api/billing/balance          billing/models.py:19-23
 *   summary  GET /api/billing/usage/summary    billing/models.py:26-42
 *   subs     GET /api/payments/subscriptions   billing/models.py:210-235
 *
 * DELIBERATELY NO SHARED VOCABULARY between scenarios. Six consumers, six
 * unrelated products. If the SDK carried a hardcoded ladder, half of these
 * would render a plan that does not exist in that consumer's catalog.
 */
(function (g) {
  'use strict';

  var F = {};

  // ---- A: a media company. Three tiers. -----------------------------------
  F.A_TIERS = { tiers: [
    { tier_id: 'hobby', display_name: 'Hobby',
      description: 'For solo work and weekend projects.', features: [],
      limits: { 'render.concurrent': { limit: 2, limit_display: '2' },
                'storage.total_gb': { limit: 50, limit_display: '50' },
                'api.requests_per_hour': { limit: 20000, limit_display: '20000' } },
      upgrade_url: '/plans' },
    { tier_id: 'studio', display_name: 'Studio',
      description: 'For small teams shipping every week.',
      features: ['gpu_access', 'priority_queue', 'shared_libraries'],
      limits: { 'render.concurrent': { limit: 40, limit_display: '40' },
                'storage.total_gb': { limit: 2000, limit_display: '2000' },
                'api.requests_per_hour': { limit: 250000, limit_display: '250000' } },
      upgrade_url: '/plans' },
    { tier_id: 'agency', display_name: 'Agency',
      description: 'For studios running client work at scale.',
      features: ['sso', 'audit_logs', 'dedicated_capacity'],
      limits: { 'render.concurrent': { limit: null, limit_display: 'Unlimited' },
                'storage.total_gb': { limit: 40000, limit_display: '40000' },
                'api.requests_per_hour': { limit: null, limit_display: 'Unlimited' } },
      upgrade_url: null }
  ]};
  F.A_USAGE = { org_id: 'org_a', tier_id: 'hobby', tier_display: 'Hobby', resources: [
    { resource_key: 'render.concurrent', display_name: 'Concurrent Renders', unit: 'renders',
      current: 2, limit: 2, utilization: 1.0, severity: 'critical', has_override: false, counter_type: 'gauge' },
    { resource_key: 'storage.total_gb', display_name: 'Project Storage', unit: 'GB',
      current: 41, limit: 50, utilization: 0.82, severity: 'warning', has_override: false, counter_type: 'gauge' },
    { resource_key: 'api.requests_per_hour', display_name: 'API Requests / Hour', unit: 'requests',
      current: 1240, limit: 20000, utilization: 0.062, severity: 'info', has_override: true, counter_type: 'rate' }
  ]};

  // ---- B/C: a logistics company. TWO tiers - the shape that broke the old
  //           hardcoded ladder. -------------------------------------------
  F.B_TIERS = { tiers: [
    { tier_id: 'free', display_name: 'Free', description: 'Get moving.', features: [],
      limits: { 'shipment.active': { limit: 5, limit_display: '5' } },
      upgrade_url: '/billing/upgrade' },
    { tier_id: 'pro', display_name: 'Pro', description: 'For growing fleets.',
      features: ['bulk_import', 'webhooks'],
      limits: { 'shipment.active': { limit: 500, limit_display: '500' } },
      upgrade_url: null }
  ]};
  F.B_DENIAL = {
    error: 'quota_exceeded', resource: 'shipment.active', current: 5, requested: 1, limit: 5,
    remaining: 0, tier: 'free', tier_display: 'Free', upgrade_url: '/billing/upgrade', retry_after: null,
    message: "You've reached the maximum of 5 shipments on the Free plan. Archive a delivered " +
             "shipment to free up a slot. Or upgrade to Pro for a higher limit. See /billing/upgrade."
  };
  F.C_USAGE = { org_id: 'org_c', tier_id: 'pro', tier_display: 'Pro', resources: [
    { resource_key: 'shipment.active', display_name: 'Active Shipments', unit: 'shipments',
      current: 470, limit: 500, utilization: 0.94, severity: 'critical', has_override: false, counter_type: 'gauge' }
  ]};

  // ---- D: exactly ONE tier. No ladder exists at all. ----------------------
  F.D_TIERS = { tiers: [
    { tier_id: 'standard', display_name: 'Standard',
      description: 'One plan. That is the whole product.', features: ['audit_logs'],
      limits: { 'seat.active': { limit: 25, limit_display: '25' } }, upgrade_url: null }
  ]};
  F.D_DENIAL = {
    error: 'quota_exceeded', resource: 'seat.active', current: 25, requested: 1, limit: 25,
    remaining: 0, tier: 'standard', tier_display: 'Standard', upgrade_url: null, retry_after: null,
    message: "You've reached the maximum of 25 seats on the Standard plan."
  };
  F.D_USAGE = { org_id: 'org_d', tier_id: 'standard', tier_display: 'Standard', resources: [
    { resource_key: 'seat.active', display_name: 'Team Seats', unit: 'seats',
      current: 25, limit: 25, utilization: 1.0, severity: 'exceeded', has_override: false, counter_type: 'gauge' }
  ]};

  // ---- E: bridge mode. /tiers is NOT mounted (setup.py:989). --------------
  F.E_DENIAL = {
    error: 'quota_exceeded', resource: 'api.requests_per_hour', current: 50000, requested: 1,
    limit: 50000, remaining: 0, tier: 'growth', tier_display: 'Growth',
    upgrade_url: 'https://example.test/upgrade', retry_after: 1800,
    message: "You've reached the maximum of 50,000 requests on the Growth plan. " +
             "Upgrade to Scale for a higher limit."
  };

  // ---- F: unlimited limits, no upgrade_url anywhere. ----------------------
  F.F_TIERS = { tiers: [
    { tier_id: 'internal', display_name: 'Internal', description: null, features: [],
      limits: {}, upgrade_url: null }
  ]};
  F.F_USAGE = { org_id: 'org_f', tier_id: 'internal', tier_display: 'Internal', resources: [
    { resource_key: 'job.queued', display_name: 'Queued Jobs', unit: 'jobs',
      current: 8231, limit: null, utilization: null, severity: 'info', has_override: false, counter_type: 'gauge' },
    { resource_key: 'job.monthly_cost', display_name: 'Monthly Compute Spend', unit: '',
      current: 1180, limit: 5000, utilization: 0.236, severity: 'info', has_override: false, counter_type: 'accumulator' }
  ]};

  // ---- K: a catalog that DOES carry TierConfig.price (models/core.py:315).
  //         `/tiers` does not serialise this today - see ticket T-14. The
  //         second tier's price omits its code on purpose: the SDK must
  //         render NO amount for it rather than guess one. -----------------
  F.K_TIERS = { tiers: [
    { tier_id: 'lite', display_name: 'Lite', description: 'Everything to start.',
      features: ['email_support'],
      limits: { 'seat.active': { limit: 3, limit_display: '3' } }, upgrade_url: '/upgrade',
      price: { amount_per_period: '19.00', currency: 'CHF', period: 'month' } },
    { tier_id: 'max', display_name: 'Max', description: 'Everything, without the ceiling.',
      features: ['email_support', 'priority_support'],
      limits: { 'seat.active': { limit: null, limit_display: 'Unlimited' } }, upgrade_url: null,
      price: { amount_per_period: '99.00', period: 'month' } }
  ]};

  // ---- billing / payment payloads ----------------------------------------
  F.INVOICES = { count: 3, has_more: true, invoices: [
    { invoice_id: 'in_1', invoice_number: 'KF-2041', status: 'open',
      subtotal: '240.00', amount_due: '240.00', amount_paid: '0.00', total_amount: '240.00',
      currency: 'NZD', due_date: '2026-08-05T00:00:00Z', created_at: '2026-07-05T00:00:00Z',
      pdf_url: '/api/payments/invoices/in_1/pdf' },
    { invoice_id: 'in_2', invoice_number: 'KF-2040', status: 'paid_in_full',
      subtotal: '240.00', amount_due: '0.00', amount_paid: '240.00', total_amount: '240.00',
      currency: 'NZD', created_at: '2026-06-05T00:00:00Z',
      pdf_url: '/api/payments/invoices/in_2/pdf' },
    // No code on this one. The amount must not render.
    { invoice_id: 'in_3', invoice_number: 'KF-2039', status: 'paid_in_full',
      subtotal: '240.00', amount_due: '0.00', amount_paid: '240.00', total_amount: '240.00',
      currency: null, created_at: '2026-05-05T00:00:00Z', pdf_url: null }
  ]};
  F.INVOICES_EMPTY = { count: 0, has_more: false, invoices: [] };

  F.BALANCE = { balance: '412.50', available_balance: '388.00', currency: 'SEK' };
  // BillingUsageSummaryResponse has total_cost but NO currency (models.py:26-42).
  F.SUMMARY = { org_id: 'org_n', total_cost: '61.20',
                period_start: '2026-07-01T00:00:00Z', period_end: '2026-07-31T00:00:00Z' };

  F.SUBS = { total: 1, has_more: false, subscriptions: [
    { subscription_id: 'sub_9', org_id: 'org_o', plan_id: 'growth_annual_v2', status: 'past_due',
      amount: 1188.0, current_period_start: '2026-01-15T00:00:00Z',
      current_period_end: '2027-01-15T00:00:00Z', cancel_at_period_end: true,
      created_at: '2025-01-15T00:00:00Z' }
  ]};
  F.SUBS_EMPTY = { total: 0, has_more: false, subscriptions: [] };

  // ---- transports ---------------------------------------------------------

  /* Quota transport. `tiers: null` simulates bridge mode (route absent -> 404). */
  function transport(usage, tiers) {
    return function (url) {
      var body = null;
      if (/\/usage$/.test(url)) body = usage;
      if (/\/tiers$/.test(url)) body = tiers;
      if (body === null || body === undefined) return respond(404, {});
      return respond(200, body);
    };
  }

  /* Route-table transport. A value of null is a 404; the string 'auth' is a
   * 401; a number is that status. Anything else is a 200 body. */
  function routes(map) {
    return function (url) {
      var hit;
      Object.keys(map).forEach(function (k) {
        if (url.slice(-k.length) === k) hit = map[k];
      });
      if (hit === 'auth') return respond(401, {});
      if (typeof hit === 'number') return respond(hit, {});
      if (hit === null || hit === undefined) return respond(404, {});
      return respond(200, hit);
    };
  }

  /* Wrap a transport so every call takes `ms` to resolve — the only way to
   * actually LOOK at the skeleton state in a browser. */
  function slow(fn, ms) {
    return function (url, init) {
      return new Promise(function (res) {
        setTimeout(function () { res(fn(url, init)); }, ms || 900);
      }).then(function (r) { return r; });
    };
  }

  function respond(status, body) {
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status: status,
      json: function () { return Promise.resolve(body); }
    });
  }

  g.AB0TDemo = { fixtures: F, transport: transport, routes: routes, slow: slow };

})(typeof window !== 'undefined' ? window : this);
