/*
 * test_sdk.js — headless proof for ab0t-quota-ui.js.
 *
 *   node sdk/test_sdk.js
 *
 * There is no browser and no jsdom on this box, so this file ships a small DOM
 * shim and drives the REAL SDK through it. It proves DOM *structure*, the
 * config-is-king rules, and (via contrast.js) the measured colour ratios of
 * the shipped stylesheet. It cannot prove layout, paint or focus behaviour —
 * see PROOF.md "Not proven".
 *
 * Scenario groups A-F are the original six catalog shapes and are UNCHANGED.
 * G-R cover the vendor widget set. X1-X4 are the structural gates.
 * Exit code 0 = all pass.
 */
'use strict';

// ---------------------------------------------------------------- DOM shim

function matches(node, sel) {
  sel = sel.trim();
  var m = /^([a-z0-9]*)((?:\.[A-Za-z0-9_-]+)*)(?:\[([^\]]+)\])?$/.exec(sel);
  if (!m) return false;
  var tag = m[1], classes = m[2] ? m[2].slice(1).split('.') : [], attr = m[3];
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  var have = (node.className || '').split(/\s+/);
  for (var i = 0; i < classes.length; i++) if (have.indexOf(classes[i]) === -1) return false;
  if (attr) { var a = attr.split('=')[0]; if (!(a in node.attrs)) return false; }
  return true;
}

function Node(tag) {
  this.tagName = String(tag).toUpperCase();
  this.childNodes = [];
  this.attrs = {};
  this.className = '';
  this.style = {};
  this.id = '';
  this._text = '';
  this.parentNode = null;
}
Object.defineProperty(Node.prototype, 'firstChild', {
  get: function () { return this.childNodes[0] || null; }
});
Object.defineProperty(Node.prototype, 'textContent', {
  get: function () {
    if (!this.childNodes.length) return this._text;
    return this.childNodes.map(function (c) { return c.textContent; }).join('');
  },
  // Browser-accurate: assigning textContent replaces the children with ONE
  // text node, so a later appendChild adds a sibling AFTER it rather than
  // shadowing it. (An earlier shim stored the string on the element itself,
  // which silently dropped it the moment anything was appended.)
  set: function (v) {
    this.childNodes = [];
    this._text = '';
    var s = String(v);
    if (s !== '') {
      var t = new Node('#text');
      t._text = s;
      t.parentNode = this;
      this.childNodes.push(t);
    }
  }
});
Node.prototype.appendChild = function (c) { c.parentNode = this; this.childNodes.push(c); return c; };
Node.prototype.removeChild = function (c) {
  var i = this.childNodes.indexOf(c);
  if (i >= 0) this.childNodes.splice(i, 1);
  return c;
};
Node.prototype.setAttribute = function (k, v) {
  this.attrs[k] = String(v);
  // SVG elements set their class via setAttribute; keep className in sync so
  // the selector shim sees them exactly as it sees HTML elements.
  if (k === 'class') this.className = String(v);
};
Node.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
Node.prototype.addEventListener = function () {};
Node.prototype.dispatchEvent = function () { return true; };
Node.prototype._walk = function (out) {
  this.childNodes.forEach(function (c) { out.push(c); c._walk(out); });
  return out;
};
Node.prototype.querySelectorAll = function (sel) {
  var all = this._walk([]);
  var list = all.filter(function (n) { return matches(n, sel); });
  list.forEach = Array.prototype.forEach.bind(list);
  return list;
};
Node.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };

var document = {
  readyState: 'complete',
  documentElement: new Node('html'),
  createElement: function (t) { return new Node(t); },
  createElementNS: function (ns, t) { var n = new Node(t); n.namespaceURI = ns; return n; },
  addEventListener: function () {},
  querySelectorAll: function () { var l = []; l.forEach = Array.prototype.forEach.bind(l); return l; },
  querySelector: function () { return null; }
};
document.documentElement.setAttribute('lang', 'en');

global.window = global;
global.document = document;
global.CustomEvent = function (n, o) { this.type = n; this.detail = o && o.detail; };
global.fetch = function () { return Promise.reject(new Error('no network in test')); };

require('./ab0t-quota-ui.js');
var UI = global.AB0TQuotaUI;
var contrast = require('./contrast.js');

// ----------------------------------------------------------- fixtures
// Deliberately no shared vocabulary between scenarios: if the SDK carried a
// hardcoded ladder, cases C-F would name a plan that does not exist.

var A_TIERS = { tiers: [
  { tier_id: 'hobby',  display_name: 'Hobby',  features: [],
    limits: { 'render.concurrent': { limit: 2, limit_display: '2' },
              'storage.total_gb': { limit: 50, limit_display: '50' } },
    upgrade_url: '/plans' },
  { tier_id: 'studio', display_name: 'Studio', features: ['gpu_access', 'priority_queue'],
    limits: { 'render.concurrent': { limit: 40, limit_display: '40' },
              'storage.total_gb': { limit: 2000, limit_display: '2000' } },
    upgrade_url: '/plans' },
  { tier_id: 'agency', display_name: 'Agency', features: ['sso'],
    limits: { 'render.concurrent': { limit: null, limit_display: 'Unlimited' } },
    upgrade_url: null }
]};
var A_USAGE = { org_id: 'org_a', tier_id: 'hobby', tier_display: 'Hobby', resources: [
  { resource_key: 'render.concurrent', display_name: 'Concurrent Renders', unit: 'renders',
    current: 2, limit: 2, utilization: 1.0, severity: 'critical', has_override: false, counter_type: 'gauge' },
  { resource_key: 'storage.total_gb', display_name: 'Project Storage', unit: 'GB',
    current: 41, limit: 50, utilization: 0.82, severity: 'warning', has_override: false, counter_type: 'gauge' },
  { resource_key: 'api.requests_per_hour', display_name: 'API Requests / Hour', unit: 'requests',
    current: 1240, limit: 20000, utilization: 0.062, severity: 'info', has_override: true, counter_type: 'rate' }
]};

var B_TIERS = { tiers: [
  { tier_id: 'free', display_name: 'Free', features: [],
    limits: { 'shipment.active': { limit: 5, limit_display: '5' } }, upgrade_url: '/billing/upgrade' },
  { tier_id: 'pro',  display_name: 'Pro',  features: ['bulk_import'],
    limits: { 'shipment.active': { limit: 500, limit_display: '500' } }, upgrade_url: null }
]};
var B_DENIAL = {
  error: 'quota_exceeded', resource: 'shipment.active', current: 5, requested: 1, limit: 5,
  remaining: 0, tier: 'free', tier_display: 'Free', upgrade_url: '/billing/upgrade', retry_after: null,
  message: "You've reached the maximum of 5 shipments on the Free plan. Archive a delivered " +
           "shipment to free up a slot. Or upgrade to Pro for a higher limit. See /billing/upgrade."
};
var C_USAGE = { org_id: 'org_c', tier_id: 'pro', tier_display: 'Pro', resources: [
  { resource_key: 'shipment.active', display_name: 'Active Shipments', unit: 'shipments',
    current: 470, limit: 500, utilization: 0.94, severity: 'critical', has_override: false, counter_type: 'gauge' }
]};

var D_TIERS = { tiers: [
  { tier_id: 'standard', display_name: 'Standard', features: [],
    limits: { 'seat.active': { limit: 25, limit_display: '25' } }, upgrade_url: null }
]};
var D_DENIAL = {
  error: 'quota_exceeded', resource: 'seat.active', current: 25, requested: 1, limit: 25,
  remaining: 0, tier: 'standard', tier_display: 'Standard', upgrade_url: null, retry_after: null,
  message: "You've reached the maximum of 25 seats on the Standard plan."
};
var D_USAGE = { org_id: 'org_d', tier_id: 'standard', tier_display: 'Standard', resources: [
  { resource_key: 'seat.active', display_name: 'Team Seats', unit: 'seats',
    current: 25, limit: 25, utilization: 1.0, severity: 'exceeded', has_override: false, counter_type: 'gauge' }
]};

var E_DENIAL = {
  error: 'quota_exceeded', resource: 'api.requests_per_hour', current: 50000, requested: 1,
  limit: 50000, remaining: 0, tier: 'growth', tier_display: 'Growth',
  upgrade_url: 'https://example.test/upgrade', retry_after: 1800,
  message: "You've reached the maximum of 50,000 requests on the Growth plan. Upgrade to Scale for a higher limit."
};

var F_TIERS = { tiers: [
  { tier_id: 'internal', display_name: 'Internal', features: [], limits: {}, upgrade_url: null }
]};
var F_USAGE = { org_id: 'org_f', tier_id: 'internal', tier_display: 'Internal', resources: [
  { resource_key: 'job.queued', display_name: 'Queued Jobs', unit: 'jobs',
    current: 8231, limit: null, utilization: null, severity: 'info', has_override: false, counter_type: 'gauge' },
  { resource_key: 'job.monthly_cost', display_name: 'Monthly Compute Spend', unit: '',
    current: 1180, limit: 5000, utilization: 0.236, severity: 'info', has_override: false, counter_type: 'accumulator' }
]};

/* K — a catalog that DOES carry price data, in the exact TierConfig.price
 * shape (models/core.py:315-332). /tiers does not serialise it today (T-14);
 * this fixture proves the renderer is ready and, critically, that a price
 * missing its code renders NO amount rather than a guessed one. */
var K_TIERS = { tiers: [
  { tier_id: 'lite', display_name: 'Lite', features: [], limits: {}, upgrade_url: '/upgrade',
    price: { amount_per_period: '19.00', currency: 'CHF', period: 'month' } },
  { tier_id: 'max', display_name: 'Max', features: [], limits: {}, upgrade_url: null,
    price: { amount_per_period: '99.00', period: 'month' } }   // NO code -> no amount
]};

/* L — invoices. Every amount carries its OWN code (billing/models.py:259).
 * The third invoice omits it deliberately. */
var L_INVOICES = { count: 3, has_more: true, invoices: [
  { invoice_id: 'in_1', invoice_number: 'KF-2041', status: 'open',
    subtotal: '240.00', amount_due: '240.00', amount_paid: '0.00', total_amount: '240.00',
    currency: 'NZD', due_date: '2026-08-05T00:00:00Z', created_at: '2026-07-05T00:00:00Z',
    pdf_url: '/api/payments/invoices/in_1/pdf' },
  { invoice_id: 'in_2', invoice_number: 'KF-2040', status: 'paid_in_full',
    subtotal: '240.00', amount_due: '0.00', amount_paid: '240.00', total_amount: '240.00',
    currency: 'NZD', created_at: '2026-06-05T00:00:00Z', pdf_url: null },
  { invoice_id: 'in_3', invoice_number: 'KF-2039', status: 'paid_in_full',
    subtotal: '240.00', amount_due: '0.00', amount_paid: '240.00', total_amount: '240.00',
    currency: null, created_at: '2026-05-05T00:00:00Z', pdf_url: null }
]};

var N_BALANCE = { balance: '412.50', available_balance: '388.00', currency: 'SEK' };
/* Summary carries total_cost but NO code (billing/models.py:26-42) — the gap
 * the widget must refuse to fill. */
var N_SUMMARY = { org_id: 'org_n', total_cost: '61.20',
                  period_start: '2026-07-01T00:00:00Z', period_end: '2026-07-31T00:00:00Z' };

var O_SUBS = { total: 1, has_more: false, subscriptions: [
  { subscription_id: 'sub_9', org_id: 'org_o', plan_id: 'growth_annual_v2', status: 'past_due',
    amount: 1188.0, current_period_start: '2026-01-15T00:00:00Z',
    current_period_end: '2027-01-15T00:00:00Z', cancel_at_period_end: true,
    created_at: '2025-01-15T00:00:00Z' }
]};

function transport(usage, tiers) {
  return function (url) {
    var body = /\/usage$/.test(url) ? usage : (/\/tiers$/.test(url) ? tiers : null);
    if (body === null || body === undefined) {
      return Promise.resolve({ ok: false, status: 404, json: function () { return Promise.resolve({}); } });
    }
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
  };
}

/* Route table transport for the billing widgets. A key mapped to null is a
 * 404 (route absent); to the string 'auth' is a 401. */
function routes(map) {
  return function (url) {
    var hit;
    Object.keys(map).forEach(function (k) {
      if (url.slice(-k.length) === k) hit = map[k];
    });
    if (hit === 'auth') {
      return Promise.resolve({ ok: false, status: 401, json: function () { return Promise.resolve({}); } });
    }
    if (hit === null || hit === undefined) {
      return Promise.resolve({ ok: false, status: 404, json: function () { return Promise.resolve({}); } });
    }
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(hit); } });
  };
}

function host() {
  var h = new Node('div');
  new Node('div').appendChild(h); // give it a parentNode
  return h;
}

// ------------------------------------------------------------- scenarios

var CASES = [
  { id: 'A', name: 'three-tier catalog, usage view',
    build: function (h) { UI.usage(h, { fetchImpl: transport(A_USAGE, A_TIERS) }); },
    check: function (h, t) { return [
      ['names the consumer next tier (Studio)', /Upgrade to Studio/.test(t)],
      ['no foreign plan names leak', !/Starter|Enterprise|Premium/.test(t)],
      ['override surfaced', /Custom limit/.test(t)],
      ['3 meters rendered', h.querySelectorAll('.ab0t-meter').length === 3],
      ['warning banner present', !!h.querySelector('.ab0t-warn')]
    ]; }},

  { id: 'B', name: 'TWO tiers, on the bottom - paywall',
    build: function (h) { UI.paywall(h, B_DENIAL, { fetchImpl: transport(null, B_TIERS) }); },
    check: function (h, t) { return [
      ['CTA names their own Pro tier', /Upgrade to Pro/.test(t)],
      ['NO invented Starter tier', !/Starter/.test(t)],
      ['action_hint preserved verbatim', /Archive a delivered shipment/.test(t)],
      ['upgrade link rendered', !!h.querySelector('a.ab0t-btn')]
    ]; }},

  { id: 'C', name: 'TWO tiers, on the TOP tier - usage',
    build: function (h) { UI.usage(h, { fetchImpl: transport(C_USAGE, B_TIERS) }); },
    check: function (h, t) { return [
      ['warning banner shown', !!h.querySelector('.ab0t-warn')],
      ['NO upgrade CTA', !h.querySelector('.ab0t-cta')],
      ['no dangling upgrade text', !/Upgrade/i.test(t)],
      ['critical severity surfaced', /Nearly at limit/.test(t)]
    ]; }},

  { id: 'D', name: 'SINGLE-tier catalog - paywall',
    build: function (h) { UI.paywall(h, D_DENIAL, { fetchImpl: transport(null, D_TIERS) }); },
    check: function (h, t) { return [
      ['message rendered', /maximum of 25 seats/.test(t)],
      ['NO CTA element', !h.querySelector('.ab0t-cta')],
      ['NO link', !h.querySelector('a')],
      ['no invented plan/upgrade copy', !/Pro|Plus|Premium|Upgrade/i.test(t)]
    ]; }},

  { id: 'E', name: 'bridge mode - /tiers returns 404',
    build: function (h) { UI.paywall(h, E_DENIAL, { fetchImpl: transport(null, null) }); },
    check: function (h, t) { return [
      ['renders with no catalog at all', /50,000 requests/.test(t)],
      ['plan chip from the 429 body', /Growth/.test(t)],
      ['generic CTA, no guessed tier name', /View upgrade options/.test(t)],
      ['retry_after surfaced', /try again in 30 minutes/.test(t)]
    ]; }},

  { id: 'F', name: 'unlimited limits, no upgrade_url',
    build: function (h) { UI.usage(h, { fetchImpl: transport(F_USAGE, F_TIERS) }); },
    check: function (h, t) {
      var meters = h.querySelectorAll('.ab0t-meter');
      var vals = h.querySelectorAll('.ab0t-meter-value');
      return [
        ['unlimited affordance shown', !!h.querySelector('.ab0t-unlimited')],
        ['no bar drawn for unlimited', meters[0].querySelectorAll('.ab0t-track').length === 0],
        ['no currency invented', !/USD|EUR|NZD/.test(t)],
        // config supplied unit:'' -> the value ends at the number, no unit invented
        ['unit-less resource omits unit', vals[1].textContent === '1,180 of 5,000'],
        // and the resource that DID supply a unit still shows it
        ['unit rendered when config supplies one', vals[0].textContent === '8,231 jobs'],
        ['NO CTA (single tier, no url)', !h.querySelector('.ab0t-cta')]
      ];
    }},

  // ------------------------------------------------- plan comparison (T-5)

  { id: 'G', name: 'PLANS - three-tier catalog, on the bottom tier',
    build: function (h) { UI.plans(h, { fetchImpl: transport(A_USAGE, A_TIERS) }); },
    check: function (h, t) {
      var cols = h.querySelectorAll('.ab0t-plan');
      var ctas = h.querySelectorAll('.ab0t-cta');
      return [
        ['one column per configured tier', cols.length === 3],
        ['exactly one current-plan marker', h.querySelectorAll('.ab0t-plan-current').length === 1],
        ['current column is the one from /usage', /Hobby/.test(cols[0].textContent)],
        ['CTA appears exactly once, on the current tier', ctas.length === 1],
        ['CTA names the NEXT array element', /Upgrade to Studio/.test(t)],
        ['limit_display rendered verbatim from the server', /Unlimited/.test(t)],
        ['row labels come from /usage display_name', /Concurrent Renders/.test(t)],
        ['no money rendered - catalog carries no price', !/[0-9]+\.[0-9]{2}/.test(t)]
      ];
    }},

  { id: 'H', name: 'PLANS - TWO tiers, customer already on the TOP tier',
    build: function (h) { UI.plans(h, { fetchImpl: transport(C_USAGE, B_TIERS) }); },
    check: function (h, t) { return [
      ['two columns', h.querySelectorAll('.ab0t-plan').length === 2],
      ['top tier marked current', h.querySelectorAll('.ab0t-plan-current').length === 1],
      ['NO CTA anywhere - nothing left to sell', h.querySelectorAll('.ab0t-cta').length === 0],
      ['no dangling upgrade text', !/Upgrade/i.test(t)]
    ]; }},

  { id: 'I', name: 'PLANS - SINGLE-tier catalog',
    build: function (h) { UI.plans(h, { fetchImpl: transport(D_USAGE, D_TIERS) }); },
    check: function (h, t) { return [
      ['exactly one column', h.querySelectorAll('.ab0t-plan').length === 1],
      ['it is marked as the current plan', h.querySelectorAll('.ab0t-plan-current').length === 1],
      ['NO CTA - there is no ladder', h.querySelectorAll('.ab0t-cta').length === 0],
      ['no second plan invented', !/Pro|Plus|Premium|Upgrade/i.test(t)]
    ]; }},

  { id: 'J', name: 'PLANS - NO catalog (bridge mode, /tiers 404)',
    build: function (h) { UI.plans(h, { fetchImpl: transport(C_USAGE, null) }); },
    check: function (h, t) { return [
      ['renders no plan card at all', !h.querySelector('.ab0t-plans')],
      ['no error shouting', !h.querySelector('.ab0t-quiet')],
      ['skeleton cleared', !h.querySelector('.ab0t-skeleton')],
      ['no text emitted', t.trim() === '']
    ]; }},

  { id: 'K', name: 'PLANS - price rendered ONLY where a code exists',
    build: function (h) { UI.plans(h, { fetchImpl: transport(null, K_TIERS) }); },
    check: function (h, t) {
      var prices = h.querySelectorAll('.ab0t-plan-price');
      return [
        ['two columns', h.querySelectorAll('.ab0t-plan').length === 2],
        ['exactly one price rendered', prices.length === 1],
        ['it used the payload code, not a default', /CHF/.test(prices[0].textContent)],
        ['the code-less price renders NO amount', !/99/.test(t)],
        ['period comes from the payload', /month/.test(prices[0].textContent)],
        ['no CTA - no /usage means no current tier', h.querySelectorAll('.ab0t-cta').length === 0]
      ];
    }},

  // ------------------------------------------------------- invoices (T-6)

  { id: 'L', name: 'INVOICES - populated list',
    build: function (h) { UI.invoices(h, { fetchImpl: routes({ '/payments/invoices': L_INVOICES }) }); },
    check: function (h, t) {
      var amounts = h.querySelectorAll('.ab0t-amount');
      return [
        ['one row per invoice', h.querySelectorAll('.ab0t-row').length === 3],
        ['invoice number rendered', /KF-2041/.test(t)],
        ['amount uses the invoice own code', /NZD| ?NZ/.test(amounts[0].textContent)],
        ['the code-less invoice renders NO amount', amounts.length === 2],
        ['outstanding row flagged arithmetically', h.querySelectorAll('.ab0t-row-due').length === 1],
        ['upstream status rendered verbatim, de-snaked', /paid in full/.test(t)],
        ['PDF link only where pdf_url exists', h.querySelectorAll('a.ab0t-file').length === 1],
        ['has_more disclosed', /most recent/.test(t)]
      ];
    }},

  { id: 'M', name: 'INVOICES - empty state',
    build: function (h) { UI.invoices(h, { fetchImpl: routes({ '/payments/invoices': { invoices: [], count: 0 } }) }); },
    check: function (h, t) { return [
      ['empty state rendered', !!h.querySelector('.ab0t-empty')],
      ['no rows', h.querySelectorAll('.ab0t-row').length === 0],
      ['says so plainly', /No invoices yet/.test(t)],
      ['no money at all', !/[0-9]/.test(t)]
    ]; }},

  { id: 'M2', name: 'INVOICES - auth required',
    build: function (h) { UI.invoices(h, { fetchImpl: routes({ '/payments/invoices': 'auth' }) }); },
    check: function (h, t) { return [
      ['renders nothing rather than a broken card', !h.querySelector('.ab0t-invoices')],
      ['no error text shown to the customer', t.trim() === ''],
      ['skeleton cleared', !h.querySelector('.ab0t-skeleton')]
    ]; }},

  { id: 'M3', name: 'INVOICES - service down, quiet retry',
    build: function (h) { UI.invoices(h, { fetchImpl: routes({ '/payments/invoices': null }) }); },
    check: function (h, t) { return [
      ['quiet error, not a red alarm', !!h.querySelector('.ab0t-quiet')],
      ['retry offered', !!h.querySelector('button.ab0t-link')],
      ['no invented figures', !/[0-9]/.test(t)]
    ]; }},

  // ------------------------------------------------- balance / spend (T-7)

  { id: 'N', name: 'BALANCE - money only where the response carries a code',
    build: function (h) {
      UI.balance(h, { fetchImpl: routes({
        '/billing/balance': N_BALANCE, '/billing/usage/summary': N_SUMMARY }) });
    },
    check: function (h, t) {
      var figs = h.querySelectorAll('.ab0t-figure-value');
      return [
        ['three figures rendered', figs.length === 3],
        ['balance uses the response code', /SEK/.test(figs[0].textContent)],
        ['available balance rendered', /388/.test(figs[1].textContent)],
        // the summary contract has no currency field - see T-16
        ['spend has no code, so no amount is shown', figs[2].textContent === '—'],
        ['and it says the amount is unavailable', /amount unavailable/i.test(t)],
        ['the balance code is NOT borrowed for spend', t.split('SEK').length - 1 === 2]
      ];
    }},

  { id: 'N2', name: 'BALANCE - summary endpoint absent',
    build: function (h) {
      UI.balance(h, { fetchImpl: routes({ '/billing/balance': N_BALANCE, '/billing/usage/summary': null }) });
    },
    check: function (h, t) { return [
      ['balance still renders', !!h.querySelector('.ab0t-balance')],
      ['only the two balance figures', h.querySelectorAll('.ab0t-figure').length === 2],
      ['no spend tile invented', !/Spend/.test(t)]
    ]; }},

  // --------------------------------------------------- subscription (T-7)

  { id: 'O', name: 'SUBSCRIPTION - active, cancelling at period end',
    build: function (h) { UI.subscription(h, { fetchImpl: routes({ '/payments/subscriptions': O_SUBS }) }); },
    check: function (h, t) { return [
      ['plan id rendered VERBATIM, never title-cased', /growth_annual_v2/.test(t)],
      ['upstream status de-snaked, not re-worded', /past due/.test(t)],
      ['non-renewal notice shown', !!h.querySelector('.ab0t-notice')],
      ['period end rendered as a date', /2027/.test(t)],
      // SubscriptionItem has `amount` but no currency field - see T-16
      ['amount NOT rendered without a code', !/1,188|1188/.test(t)]
    ]; }},

  { id: 'P', name: 'SUBSCRIPTION - none',
    build: function (h) { UI.subscription(h, { fetchImpl: routes({ '/payments/subscriptions': { subscriptions: [] } }) }); },
    check: function (h, t) { return [
      ['empty state rendered', !!h.querySelector('.ab0t-empty')],
      ['says so plainly', /No active subscription/.test(t)],
      ['no plan name invented', !/Pro|Plus|Premium/i.test(t)]
    ]; }},

  // --------------------------------------------- money contract + a11y

  { id: 'Q', name: 'MONEY CONTRACT - refuses to invent a currency',
    build: function () {},
    check: function () {
      var m = UI._internal.money;
      return [
        ['no code -> null', m('10.00', null) === null],
        ['empty code -> null', m('10.00', '') === null],
        ['malformed code -> null', m('10.00', 'dollars') === null],
        ['non-numeric amount -> null', m('n/a', 'NZD') === null],
        ['null amount -> null', m(null, 'NZD') === null],
        ['valid pair -> a formatted amount containing the code or its symbol',
          typeof m('10.00', 'NZD') === 'string' && /10/.test(m('10.00', 'NZD'))],
        ['zero is a real amount, not a missing one', typeof m('0.00', 'NZD') === 'string']
      ];
    }},

  { id: 'R', name: 'A11Y - meters expose progressbar semantics',
    build: function (h) { UI.usage(h, { fetchImpl: transport(A_USAGE, A_TIERS) }); },
    check: function (h) {
      var tracks = h.querySelectorAll('.ab0t-track');
      var ok = tracks.length > 0;
      tracks.forEach(function (tr) {
        if (tr.getAttribute('role') !== 'progressbar') ok = false;
        if (tr.getAttribute('aria-valuemin') === null) ok = false;
        if (tr.getAttribute('aria-valuemax') === null) ok = false;
        if (tr.getAttribute('aria-valuenow') === null) ok = false;
        if (!tr.getAttribute('aria-valuetext')) ok = false;
      });
      var live = h.querySelectorAll('.ab0t-sr');
      return [
        ['every track is a progressbar with min/max/now/valuetext', ok],
        ['a persistent polite live region exists', live.length === 1 &&
          live[0].getAttribute('aria-live') === 'polite'],
        ['it names the STATE, not a plan', /Nearly at limit/.test(live[0].textContent) &&
          !/Upgrade/.test(live[0].textContent)],
        ['severity carries a text label, not colour alone',
          h.querySelectorAll('.ab0t-badge').length === 3],
        ['icons are aria-hidden',
          h.querySelectorAll('.ab0t-i').every(function (i) { return i.getAttribute('aria-hidden') === 'true'; })]
      ];
    }},

  { id: 'S', name: 'CATALOG-INDEPENDENCE - billing widgets ignore the tier shape',
    build: function (h) {
      // Same invoice payload, three different catalog worlds (two-tier,
      // single-tier, no catalog). The rendered output must be identical:
      // these widgets carry none of the config-is-king risk, and this is the
      // assertion that keeps it that way.
      h.__variants = [B_TIERS, D_TIERS, null].map(function (tiers) {
        var v = host();
        UI.invoices(v, { fetchImpl: routes({
          '/payments/invoices': L_INVOICES, '/tiers': tiers }) });
        return v;
      });
    },
    check: function (h) {
      var texts = h.__variants.map(function (v) { return v.textContent; });
      return [
        ['renders under a two-tier catalog', /KF-2041/.test(texts[0])],
        ['renders under a single-tier catalog', /KF-2041/.test(texts[1])],
        ['renders with no catalog at all', /KF-2041/.test(texts[2])],
        ['output is byte-identical across all three',
          texts[0] === texts[1] && texts[1] === texts[2]]
      ];
    }}
];

// ---------------------------------------------------------------- runner

var hosts = CASES.map(function (c) { var h = host(); c.build(h); return h; });

setTimeout(function () {
  var failed = 0, checks = 0, passedChecks = 0;
  console.log('\nab0t-quota-ui v' + UI.version + ' — proof obligations (DESIGN §7)\n');
  CASES.forEach(function (c, i) {
    var h = hosts[i], t = h.textContent;
    var rows = c.check(h, t);
    var ok = rows.every(function (r) { return r[1]; });
    if (!ok) failed++;
    console.log((ok ? '  PASS' : '  FAIL') + '  ' + c.id + ' · ' + c.name);
    rows.forEach(function (r) {
      checks++;
      if (r[1]) passedChecks++;
      console.log('          ' + (r[1] ? '✓ ' : '✗ ') + r[0]);
    });
  });

  // ------------------------------------------------------ structural gates
  //
  // These test the ARTIFACT, not the intent, which is why they survive
  // refactors. Comments are stripped first: the file is allowed to *discuss*
  // the words it may not *ship*.

  var src = require('fs').readFileSync(__dirname + '/ab0t-quota-ui.js', 'utf8');
  var code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

  // Every string/template literal in the shipped code. A value only reaches
  // the customer's screen through one of these.
  var literals = code.match(/'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`/g) || [];
  var literalText = literals.join('\n');

  var gates = [];

  gates.push(['X1', 'no tier name or plan ladder in the shipped bundle',
    [/\bFree\b/, /\bStarter\b/, /\bPro\b/, /\bEnterprise\b/, /\bPremium\b/, /\bBasic\b/]
      .filter(function (re) { return re.test(code); })]);

  // D-V456-7: money is allowed, INVENTED money is not. A currency symbol or
  // ISO code can only be shipped inside a string literal, so that is exactly
  // where we look — `currency: code` (a variable) passes, `currency: 'USD'`
  // does not.
  gates.push(['X2', 'no currency symbol or ISO code literal, and no default code',
    [/[$€£¥₹]/, /\b(USD|EUR|GBP|JPY|AUD|CAD|NZD|CHF)\b/]
      .filter(function (re) { return re.test(literalText); })
      .concat(/currency\s*[:=]\s*['"`]/.test(code) ? [/currency\s*[:=]\s*['"`]/] : [])]);

  // Upstream status vocabulary (Stripe's, via payment) must never be branched
  // on or re-worded client-side — the provider adds values without asking us.
  gates.push(['X3', 'no upstream status vocabulary hardcoded',
    [/\b(paid|unpaid|past_due|trialing|canceled|cancelled|voided|drafted)\b/i]
      .filter(function (re) { return re.test(literalText); })]);

  gates.forEach(function (g) {
    var leaks = g[2];
    if (leaks.length) failed++;
    checks++; if (!leaks.length) passedChecks++;
    console.log('\n  ' + (leaks.length ? 'FAIL' : 'PASS') + '  ' + g[0] + ' · ' + g[1] +
      (leaks.length ? ' — leaked: ' + leaks.map(function (r) { return r.source; }).join(', ') : ''));
  });

  // X4 — measured WCAG contrast for every pair the components actually stack,
  // in BOTH themes, parsed out of the shipped stylesheet.
  var cr = contrast.check();
  var worstText = Math.min.apply(null, cr.rows.filter(function (r) { return r.min >= 4.5; })
    .map(function (r) { return r.ratio; }));
  var worstNon = Math.min.apply(null, cr.rows.filter(function (r) { return r.min < 4.5; })
    .map(function (r) { return r.ratio; }));
  if (cr.failures.length) failed++;
  checks++; if (!cr.failures.length) passedChecks++;
  console.log('\n  ' + (cr.failures.length ? 'FAIL' : 'PASS') +
    '  X4 · WCAG AA contrast, ' + cr.rows.length + ' pairs across both themes' +
    ' — worst text ' + worstText.toFixed(2) + ':1 (min 4.5), ' +
    'worst non-text ' + worstNon.toFixed(2) + ':1 (min 3.0)');
  cr.failures.forEach(function (f) { console.log('          ✗ ' + f); });

  // X5 — the white-label claim, verified rather than asserted. Each demo brand
  // overrides SOME tokens and inherits the rest; merged with the base theme,
  // every pair must still clear AA. A brand that recolours its surfaces and
  // silently breaks the severity colours it did not touch fails here.
  var brandFails = [], brandPairs = 0;
  [['.brand-atlas,', ':root[data-theme="dark"] .brand-atlas'],
   ['.brand-quanta,', ':root[data-theme="dark"] .brand-quanta']].forEach(function (b) {
    var br = contrast.checkBrand(__dirname + '/demo-brands.css', null, b[0], b[1]);
    brandPairs += br.rows.length;
    brandFails = brandFails.concat(br.failures);
  });
  if (brandFails.length) failed++;
  checks++; if (!brandFails.length) passedChecks++;
  console.log('\n  ' + (brandFails.length ? 'FAIL' : 'PASS') +
    '  X5 · white-label brands stay AA, ' + brandPairs +
    ' pairs across 2 brands x 2 themes');
  brandFails.forEach(function (f) { console.log('          ✗ ' + f); });

  var groups = CASES.length + gates.length + 2;
  console.log('\n  ' + (groups - failed) + ' / ' + groups + ' groups pass (' +
    passedChecks + ' / ' + checks + ' assertions)\n');
  process.exit(failed ? 1 : 0);
}, 120);
