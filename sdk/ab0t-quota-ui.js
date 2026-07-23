/*!
 * ab0t-quota-ui.js — the commercial UI layer for ab0t-quota.
 * v0.2.0 · zero dependencies · no build step · no external fetch · MIT-internal
 *
 * THE CONFIG IS KING. This file contains no tier name, no plan ladder, no
 * resource copy, no currency and no price. Every noun it renders arrives over
 * the wire from the consumer's own quota-config.json:
 *
 *   plan name      tiers[].display_name        (setup.py:2371)
 *   resource name  resources[].display_name    (engine.py:1167)
 *   unit           resources[].unit            (engine.py:1168)
 *   the sentence   429 .message                (models/responses.py:116)
 *   upgrade target NEXT element of /tiers      (pre-sorted, setup.py:2363)
 *   the CTA link   .upgrade_url                (models/responses.py:114)
 *   money          ONLY a field that carries both an amount AND a code
 *                  (InvoiceItem.currency, BillingBalanceResponse.currency —
 *                   billing/models.py:22,259). See `money()` below.
 *
 * If you are about to add a string that names a plan, a resource, a price, a
 * currency or an upstream status vocabulary: stop. It belongs in the
 * consumer's config or in the API response, and shipping it here reproduces
 * the exact defect removed from the library in 0.6.3. `test_sdk.js` group G
 * greps this artifact and fails the build if you do.
 *
 * Icons are inline SVG drawn by this file — no icon font, no sprite sheet, no
 * network request. The bundle is two files and stays that way.
 */
(function (global) {
  'use strict';

  // ---------------------------------------------------------------- utils

  var DEFAULTS = {
    base: '/api/quotas',       // quota routes  (setup.py:412)
    billingBase: '/api',       // billing/payment proxy routes (router.py:93)
    fetchImpl: null,
    credentials: 'same-origin'
  };

  function cfg(opts) {
    var o = {}, k;
    for (k in DEFAULTS) o[k] = DEFAULTS[k];
    for (k in (opts || {})) if (opts[k] !== undefined) o[k] = opts[k];
    return o;
  }

  function locale() {
    var l = document.documentElement.getAttribute('lang');
    return l || undefined; // undefined => Intl uses the browser default
  }

  /* Plain numbers. `limit` may legitimately be null => unlimited. */
  function num(v) {
    if (v === null || v === undefined || isNaN(v)) return '';
    try { return new Intl.NumberFormat(locale()).format(v); } catch (e) { return String(v); }
  }

  /*
   * MONEY — the one place the SDK is allowed to render an amount, and it
   * refuses unless the payload carried BOTH the number and the ISO code.
   *
   * There is no default code, no symbol table and no locale inference. A
   * response that omits the code gets NOTHING rendered rather than a plausible
   * guess: showing a customer the wrong currency on a balance is a materially
   * worse failure than showing them no balance (D-V456-7).
   *
   * Amounts arrive as decimal STRINGS on the billing/payment models
   * (billing/models.py:20-21,255-258) — parsed, never eval'd, and dropped if
   * they do not parse.
   */
  function money(amount, code) {
    if (amount === null || amount === undefined || amount === '') return null;
    var n = (typeof amount === 'number') ? amount : parseFloat(String(amount));
    if (!isFinite(n)) return null;
    if (!code || typeof code !== 'string' || !/^[A-Za-z]{3}$/.test(code.trim())) return null;
    var c = code.trim().toUpperCase();
    try {
      return new Intl.NumberFormat(locale(), {
        style: 'currency', currency: c
      }).format(n);
    } catch (e) {
      // Intl rejected the code: show the code beside the number rather than
      // dropping the amount entirely. Still never a symbol we invented.
      return c + ' ' + num(n);
    }
  }

  /* ISO-8601 in, localised date out. Unparseable in, nothing out. */
  function date(iso) {
    if (!iso || typeof iso !== 'string') return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    try {
      return new Intl.DateTimeFormat(locale(), {
        year: 'numeric', month: 'short', day: 'numeric'
      }).format(d);
    } catch (e) { return iso.slice(0, 10); }
  }

  /* Consumer-authored identifiers (feature flags, resource keys, upstream
   * status values) are de-snaked for display and otherwise left exactly as
   * their author wrote them. We never title-case a product noun — that is the
   * `.title()`-mangling defect called out in TICKET_config_is_king §5e. */
  function desnake(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[_-]+/g, ' ');
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function emit(node, name, detail) {
    node.dispatchEvent(new CustomEvent('ab0t:' + name, { detail: detail, bubbles: true }));
  }

  /* Only ever attach a link when the config actually supplied one. A dead
   * href="#" is worse than no button. */
  function safeUrl(u) {
    if (!u || typeof u !== 'string') return null;
    var t = u.trim();
    if (!t) return null;
    if (t.charAt(0) === '/' && t.charAt(1) !== '/') return t;      // same-origin path
    if (/^https?:\/\//i.test(t)) return t;                          // absolute http(s)
    return null;                                                    // javascript:, data:, //evil
  }

  function get(o, k, dflt) {
    return (o && o[k] !== undefined && o[k] !== null) ? o[k] : dflt;
  }

  // ------------------------------------------------------------------ icons
  /*
   * Inline SVG, 24x24, stroked with currentColor so a brand's token colours
   * flow straight through. Every icon is aria-hidden: it is the third channel
   * behind a text label and a distinct SHAPE, never the only signal.
   */

  var SVG_NS = 'http://www.w3.org/2000/svg';

  var ICONS = {
    // Severity — four unmistakably different silhouettes, readable in
    // greyscale and at 14px.
    ok:        [['circle', { cx: 12, cy: 12, r: 9 }], ['path', { d: 'M8 12.5l2.6 2.6L16 9.7' }]],
    warn:      [['path', { d: 'M12 3.6L21.2 20H2.8L12 3.6z' }], ['path', { d: 'M12 9.6v4.2' }],
                ['path', { d: 'M12 17.1h.01' }]],
    critical:  [['path', { d: 'M12 2.8l9.2 9.2-9.2 9.2L2.8 12z' }], ['path', { d: 'M12 7.8v4.6' }],
                ['path', { d: 'M12 16h.01' }]],
    exceeded:  [['path', { d: 'M8.2 2.8h7.6L21.2 8.2v7.6L15.8 21.2H8.2L2.8 15.8V8.2z' }],
                ['path', { d: 'M8.6 8.6l6.8 6.8' }], ['path', { d: 'M15.4 8.6l-6.8 6.8' }]],
    infinity:  [['path', { d: 'M7.2 8.4c-2.3 0-4.2 1.6-4.2 3.6s1.9 3.6 4.2 3.6c3.6 0 5.4-7.2 9.6-7.2 2.3 0 4.2 1.6 4.2 3.6s-1.9 3.6-4.2 3.6c-4.2 0-6-7.2-9.6-7.2z' }]],
    arrow:     [['path', { d: 'M4.5 12h14' }], ['path', { d: 'M13 6.5l5.5 5.5-5.5 5.5' }]],
    spinner:   [['path', { d: 'M12 3.2v3.4' }], ['path', { d: 'M12 17.4v3.4' }],
                ['path', { d: 'M3.2 12h3.4' }], ['path', { d: 'M17.4 12h3.4' }],
                ['path', { d: 'M5.8 5.8l2.4 2.4' }], ['path', { d: 'M15.8 15.8l2.4 2.4' }],
                ['path', { d: 'M18.2 5.8l-2.4 2.4' }], ['path', { d: 'M8.2 15.8l-2.4 2.4' }]],
    refresh:   [['path', { d: 'M20.4 11.2a8.4 8.4 0 10-1.9 6.1' }], ['path', { d: 'M20.8 5.6v5.6h-5.6' }]],
    download:  [['path', { d: 'M12 3.8v10.4' }], ['path', { d: 'M7.6 10l4.4 4.4L16.4 10' }],
                ['path', { d: 'M4.4 19.4h15.2' }]],
    receipt:   [['path', { d: 'M5.4 2.8h13.2v18.4l-2.6-1.6-2.6 1.6-2.4-1.6-2.6 1.6-3-1.6z' }],
                ['path', { d: 'M9 8h6' }], ['path', { d: 'M9 12h6' }]],
    wallet:    [['path', { d: 'M3.4 7.4A2.6 2.6 0 016 4.8h11.4v2.6' }],
                ['path', { d: 'M3.4 7.4v9.8a2.6 2.6 0 002.6 2.6h13a2 2 0 002-2v-8a2 2 0 00-2-2H3.4z' }],
                ['path', { d: 'M17.6 12.8h.01' }]],
    calendar:  [['path', { d: 'M4.4 6.6h15.2v13.2H4.4z' }], ['path', { d: 'M4.4 10.6h15.2' }],
                ['path', { d: 'M8.4 3.8v3.6' }], ['path', { d: 'M15.6 3.8v3.6' }]],
    inbox:     [['path', { d: 'M3.4 13.4h5l1.6 2.6h4l1.6-2.6h5' }],
                ['path', { d: 'M5.6 4.6h12.8l2.2 8.8v5.4a1.6 1.6 0 01-1.6 1.6H5a1.6 1.6 0 01-1.6-1.6v-5.4z' }]],
    external:  [['path', { d: 'M13.4 4.6h6v6' }], ['path', { d: 'M19.4 4.6L10 14' }],
                ['path', { d: 'M17.2 13.6v5a1.8 1.8 0 01-1.8 1.8H5.4a1.8 1.8 0 01-1.8-1.8V8.6a1.8 1.8 0 011.8-1.8h5' }]]
  };

  function icon(name, extraCls) {
    var spec = ICONS[name] || ICONS.ok;
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('class', 'ab0t-i' + (extraCls ? ' ' + extraCls : ''));
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    spec.forEach(function (s) {
      var n = document.createElementNS(SVG_NS, s[0]);
      for (var k in s[1]) n.setAttribute(k, String(s[1][k]));
      svg.appendChild(n);
    });
    return svg;
  }

  // ------------------------------------------------------------- transport

  function api(conf, path, base) {
    var f = conf.fetchImpl || global.fetch.bind(global);
    return f((base || conf.base) + path, {
      credentials: conf.credentials,
      headers: { 'Accept': 'application/json' }
    }).then(function (r) {
      if (r.status === 401 || r.status === 403) { var e = new Error('auth'); e.code = 'auth'; throw e; }
      if (!r.ok) { var e2 = new Error('http_' + r.status); e2.code = 'http'; throw e2; }
      return r.json();
    });
  }

  // ------------------------------------------------------------ the ladder
  /*
   * THE ENTIRE UPGRADE ALGORITHM. UNCHANGED SINCE v0.1.0 — every new component
   * below calls this rather than reimplementing it (D-V456-5).
   *
   * /tiers is returned pre-sorted by sort_order (setup.py:2363), so "the next
   * tier" is literally the next array element. We do not re-sort, do not rank
   * by name, do not assume how many tiers exist.
   *
   * Consequences that fall out for free, with no special-casing:
   *   - single-tier catalog        -> index 0 is last     -> null (no CTA)
   *   - two-tier, on the top tier  -> index 1 is last     -> null (no CTA)
   *   - two-tier, on the bottom    -> returns tiers[1], whatever it is called
   *   - catalog unavailable (bridge mode, setup.py:989) -> null, paywall still works
   */
  function nextTier(tiers, currentTierId) {
    if (!tiers || !tiers.length) return null;
    for (var i = 0; i < tiers.length; i++) {
      if (tiers[i].tier_id === currentTierId) {
        return (i + 1 < tiers.length) ? tiers[i + 1] : null;
      }
    }
    return null; // current tier not in catalog: say nothing rather than guess
  }

  // -------------------------------------------------- mount area + a11y

  /*
   * Each mount point gets a content area (re-rendered freely) and ONE
   * persistent polite live region that survives re-renders — a live region
   * that is destroyed and recreated announces nothing. State changes write a
   * short sentence into it; nothing else does.
   */
  function mountArea(node) {
    if (!node.__ab0tArea) {
      var area = el('div', 'ab0t-area');
      var live = el('div', 'ab0t-sr');
      live.setAttribute('role', 'status');
      live.setAttribute('aria-live', 'polite');
      node.appendChild(area);
      node.appendChild(live);
      node.__ab0tArea = area;
      node.__ab0tLive = live;
    }
    return node.__ab0tArea;
  }

  function announce(node, text) {
    if (node.__ab0tLive) node.__ab0tLive.textContent = String(text || '');
  }

  /* Skeleton — shown between mount and first paint so the widget occupies its
   * final height immediately and the page does not jump. */
  function skeleton(area, rows) {
    clear(area);
    var sk = el('div', 'ab0t-skeleton');
    sk.setAttribute('aria-hidden', 'true');
    sk.appendChild(el('div', 'ab0t-sk ab0t-sk-title'));
    for (var i = 0; i < (rows || 3); i++) {
      sk.appendChild(el('div', 'ab0t-sk ab0t-sk-row'));
      sk.appendChild(el('div', 'ab0t-sk ab0t-sk-bar'));
    }
    area.appendChild(sk);
  }

  /* The one error affordance. A billing widget must never shout during an
   * outage: quiet line, working retry, no red. */
  function quietError(area, onRetry) {
    clear(area);
    var q = el('div', 'ab0t-quiet');
    q.appendChild(icon('refresh'));
    q.appendChild(el('span', null, 'Unavailable right now.'));
    var btn = el('button', 'ab0t-link', 'Retry');
    btn.type = 'button';
    btn.addEventListener('click', onRetry);
    q.appendChild(btn);
    area.appendChild(q);
  }

  function emptyState(area, iconName, text) {
    clear(area);
    var e = el('div', 'ab0t-empty');
    e.appendChild(icon(iconName));
    e.appendChild(el('span', null, text));
    area.appendChild(e);
  }

  function cardHead(title, chipText) {
    var head = el('div', 'ab0t-card-head');
    head.appendChild(el('h3', 'ab0t-card-title', title));
    if (chipText) head.appendChild(el('span', 'ab0t-plan-chip', chipText));
    return head;
  }

  // --------------------------------------------------------------- meters

  var SEVERITY_RANK = { info: 0, warning: 1, critical: 2, exceeded: 3 };

  /* Severity is never colour-only (DESIGN §4). Each level carries a text
   * label AND a distinct icon shape. These are the SDK's only fixed strings,
   * and they name a STATE, not a product noun. */
  var SEVERITY_LABEL = { info: 'OK', warning: 'Approaching limit', critical: 'Nearly at limit', exceeded: 'Limit reached' };
  var SEVERITY_ICON = { info: 'ok', warning: 'warn', critical: 'critical', exceeded: 'exceeded' };

  function meter(res) {
    var limit = get(res, 'limit', null);
    var current = get(res, 'current', 0);
    var unit = get(res, 'unit', '');
    var sev = get(res, 'severity', 'info');
    var unlimited = (limit === null);

    var row = el('div', 'ab0t-meter ab0t-sev-' + sev);
    row.setAttribute('data-resource', get(res, 'resource_key', ''));

    var head = el('div', 'ab0t-meter-head');
    head.appendChild(el('span', 'ab0t-meter-name', get(res, 'display_name', res.resource_key)));

    // "3 of 25 sandboxes" / "3 sandboxes" when unlimited. Unit may be absent
    // in config -> we omit it rather than inventing one.
    var value = unlimited
      ? num(current) + (unit ? ' ' + unit : '')
      : num(current) + ' of ' + num(limit) + (unit ? ' ' + unit : '');
    head.appendChild(el('span', 'ab0t-meter-value', value));
    row.appendChild(head);

    if (unlimited) {
      // No bar. A full bar and an empty bar would both be lies.
      var un = el('div', 'ab0t-unlimited');
      un.appendChild(icon('infinity'));
      un.appendChild(el('span', null, 'No limit on this plan'));
      row.appendChild(un);
      return row;
    }

    var pct = (typeof res.utilization === 'number')
      ? Math.max(0, Math.min(1, res.utilization))
      : (limit > 0 ? Math.max(0, Math.min(1, current / limit)) : 0);

    var track = el('div', 'ab0t-track');
    track.setAttribute('role', 'progressbar');
    track.setAttribute('aria-valuemin', '0');
    track.setAttribute('aria-valuemax', String(limit));
    track.setAttribute('aria-valuenow', String(current));
    // valuetext is the human sentence, not the number (DESIGN §4).
    track.setAttribute('aria-valuetext', value + ' — ' + (SEVERITY_LABEL[sev] || sev));
    track.setAttribute('aria-label', get(res, 'display_name', res.resource_key));
    var fill = el('div', 'ab0t-fill');
    fill.style.width = (pct * 100).toFixed(1) + '%';
    track.appendChild(fill);
    row.appendChild(track);

    var foot = el('div', 'ab0t-meter-foot');
    var badge = el('span', 'ab0t-badge');
    badge.appendChild(icon(SEVERITY_ICON[sev] || 'ok'));
    badge.appendChild(el('span', null, SEVERITY_LABEL[sev] || sev));
    foot.appendChild(badge);
    if (get(res, 'has_override', false)) {
      foot.appendChild(el('span', 'ab0t-override', 'Custom limit'));
    }
    row.appendChild(foot);
    return row;
  }

  // --------------------------------------------------------- upgrade CTA

  /*
   * Renders the upgrade affordance, or NOTHING AT ALL.
   *
   * Nothing is a correct and common outcome: the top tier of any catalog, a
   * single-tier catalog, or a consumer who configured no upgrade_url. In all
   * of those the component must be silent — not disabled, not "contact us",
   * not a dangling sentence. Silence is the feature.
   */
  function upgradeCta(next, url) {
    var href = safeUrl(url);
    if (!next && !href) return null;

    var wrap = el('div', 'ab0t-cta');
    if (href) {
      var a = el('a', 'ab0t-btn');
      a.setAttribute('href', href);
      // The button names the consumer's own next plan, or stays generic if
      // the catalog is unavailable (bridge mode) but a URL was supplied.
      a.appendChild(el('span', null,
        next ? ('Upgrade to ' + next.display_name) : 'View upgrade options'));
      a.appendChild(icon('arrow', 'ab0t-i-arrow'));
      wrap.appendChild(a);
    } else if (next) {
      // Catalog knows the next plan but config gave no URL: state the fact,
      // offer no dead link.
      wrap.appendChild(el('span', 'ab0t-cta-text', next.display_name + ' has a higher limit.'));
    }
    return wrap;
  }

  function tierFeatures(next) {
    if (!next || !next.features || !next.features.length) return null;
    var ul = el('ul', 'ab0t-features');
    next.features.forEach(function (f) {
      // Feature flags are consumer-authored identifiers (TierConfig.features,
      // models/core.py:440). We de-snake them for display and otherwise leave
      // them exactly as the consumer wrote them.
      var li = el('li');
      li.appendChild(icon('ok'));
      li.appendChild(el('span', null, desnake(f)));
      ul.appendChild(li);
    });
    return ul;
  }

  // ------------------------------------------------------------- PAYWALL

  /*
   * The revenue moment. Input is the raw 429 body from
   * models/responses.py:103-117 — exactly what the consumer's endpoint
   * already returns today, with no transformation.
   *
   * `.message` is rendered VERBATIM. It was composed server-side by
   * MessageBuilder.deny() from the consumer's own catalog (messages.py) and
   * already names their plan, their resource, their unit, their action_hint
   * and their next tier. Re-deriving it here would create a second copy of
   * the copy, which D-CK-2 exists to prevent.
   */
  function paywall(target, payload, opts) {
    var conf = cfg(opts);
    var node = (typeof target === 'string') ? document.querySelector(target) : target;
    if (!node || !payload) return null;
    var area = mountArea(node);

    function render(tiers) {
      clear(area);
      var next = nextTier(tiers, get(payload, 'tier', null));

      var card = el('div', 'ab0t-paywall');
      card.setAttribute('role', 'alertdialog');
      card.setAttribute('aria-modal', 'false');
      card.setAttribute('aria-labelledby', 'ab0t-pw-msg');

      var head = el('div', 'ab0t-paywall-head');
      head.appendChild(icon(SEVERITY_ICON.exceeded, 'ab0t-i-lg'));
      // tier_display is the consumer's own plan name (responses.py:113).
      var td = get(payload, 'tier_display', null);
      if (td) head.appendChild(el('span', 'ab0t-plan-chip', td));
      card.appendChild(head);

      var msg = el('p', 'ab0t-paywall-msg', get(payload, 'message', ''));
      msg.id = 'ab0t-pw-msg';
      card.appendChild(msg);

      // The bar gives the sentence a shape. Only when we have real numbers.
      var lim = get(payload, 'limit', null), cur = get(payload, 'current', null);
      if (typeof lim === 'number' && lim > 0 && typeof cur === 'number') {
        var track = el('div', 'ab0t-track ab0t-track-full');
        track.setAttribute('role', 'progressbar');
        track.setAttribute('aria-valuemin', '0');
        track.setAttribute('aria-valuemax', String(lim));
        track.setAttribute('aria-valuenow', String(cur));
        track.setAttribute('aria-valuetext', num(cur) + ' of ' + num(lim) + ' — ' + SEVERITY_LABEL.exceeded);
        var fill = el('div', 'ab0t-fill');
        fill.style.width = '100%';
        track.appendChild(fill);
        card.appendChild(track);
        card.appendChild(el('div', 'ab0t-paywall-nums', num(cur) + ' of ' + num(lim)));
      }

      var feats = tierFeatures(next);
      if (feats) {
        card.appendChild(el('div', 'ab0t-feat-label',
          next.display_name + ' also includes'));
        card.appendChild(feats);
      }

      var cta = upgradeCta(next, get(payload, 'upgrade_url', null));
      if (cta) {
        card.appendChild(cta);
        // CTA first in tab order (DESIGN §4).
        var link = cta.querySelector('a');
        if (link) setTimeout(function () { try { link.focus(); } catch (e) {} }, 0);
      }

      // RATE-limited resources recover on their own; say so instead of
      // selling an upgrade the user may not need.
      var ra = get(payload, 'retry_after', null);
      if (typeof ra === 'number' && ra > 0) {
        card.appendChild(el('p', 'ab0t-retry', 'You can try again in ' + retryPhrase(ra) + '.'));
      }

      area.appendChild(card);
      announce(node, SEVERITY_LABEL.exceeded + '.');
      emit(node, 'paywall-shown', { resource: get(payload, 'resource', null), next: next });
    }

    // The catalog is a BONUS, never a precondition: /tiers is not mounted in
    // bridge mode (setup.py:989). Render immediately from the 429 body, then
    // enrich if the catalog turns up.
    render(null);
    api(conf, '/tiers')
      .then(function (d) { render(get(d, 'tiers', null)); })
      .catch(function () { /* already rendered correctly without it */ });

    return node;
  }

  function retryPhrase(s) {
    if (s < 60) return num(s) + ' seconds';
    var m = Math.round(s / 60);
    if (m < 60) return num(m) + (m === 1 ? ' minute' : ' minutes');
    var h = Math.round(m / 60);
    return num(h) + (h === 1 ? ' hour' : ' hours');
  }

  // --------------------------------------------------------------- USAGE

  function renderUsage(node, usage, tiers) {
    var area = mountArea(node);
    clear(area);
    var resources = get(usage, 'resources', []);

    // Empty is silence, not "no data" (DESIGN §3).
    if (!resources.length) return;

    var card = el('section', 'ab0t-usage');
    card.setAttribute('aria-label', 'Usage');

    var head = el('div', 'ab0t-usage-head');
    head.appendChild(el('h3', 'ab0t-usage-title', 'Your usage'));
    var td = get(usage, 'tier_display', null);
    if (td) head.appendChild(el('span', 'ab0t-plan-chip', td));
    card.appendChild(head);

    // Pre-block warning: the best conversion moment in the product, and the
    // one thing the middleware path currently drops (information doc §3).
    var worst = null;
    resources.forEach(function (r) {
      var s = get(r, 'severity', 'info');
      if ((SEVERITY_RANK[s] || 0) >= 1) {
        if (!worst || SEVERITY_RANK[s] > SEVERITY_RANK[worst.severity]) worst = r;
      }
    });

    if (worst) {
      var next = nextTier(tiers, get(usage, 'tier_id', null));
      var banner = el('div', 'ab0t-warn ab0t-sev-' + worst.severity);
      banner.setAttribute('role', 'status');
      var b1 = el('div', 'ab0t-warn-body');
      b1.appendChild(icon(SEVERITY_ICON[worst.severity] || 'warn'));
      var pct = (typeof worst.utilization === 'number') ? Math.round(worst.utilization * 100) : null;
      b1.appendChild(el('span', null,
        get(worst, 'display_name', worst.resource_key) + ': ' +
        num(worst.current) + ' of ' + num(worst.limit) +
        (worst.unit ? ' ' + worst.unit : '') +
        (pct !== null ? ' (' + pct + '%)' : '')));
      banner.appendChild(b1);
      var cta = upgradeCta(next, tierUrl(tiers, get(usage, 'tier_id', null)));
      if (cta) banner.appendChild(cta);
      card.appendChild(banner);
      emit(node, 'quota-warning', { resource: worst.resource_key, severity: worst.severity });
    }

    var list = el('div', 'ab0t-meters');
    resources.forEach(function (r) { list.appendChild(meter(r)); });
    card.appendChild(list);

    area.appendChild(card);

    // Live-region sentence names the state, never a product noun.
    announce(node, worst
      ? (SEVERITY_LABEL[worst.severity] + ': ' + get(worst, 'display_name', worst.resource_key))
      : 'Usage updated.');
  }

  function tierUrl(tiers, tierId) {
    if (!tiers) return null;
    for (var i = 0; i < tiers.length; i++) {
      if (tiers[i].tier_id === tierId) return tiers[i].upgrade_url || null;
    }
    return null;
  }

  function usage(target, opts) {
    var conf = cfg(opts);
    var node = (typeof target === 'string') ? document.querySelector(target) : target;
    if (!node) return null;
    var area = mountArea(node);

    function fail(kind) {
      if (kind === 'auth') { clear(area); emit(node, 'auth-required', {}); return; }
      quietError(area, load);
    }

    function load() {
      skeleton(area, 3);
      Promise.all([
        api(conf, '/usage'),
        api(conf, '/tiers').catch(function () { return null; }) // optional
      ]).then(function (r) {
        renderUsage(node, r[0], r[1] ? get(r[1], 'tiers', null) : null);
      }).catch(function (e) { fail(e && e.code); });
    }

    load();
    node.ab0tRefresh = load;
    return node;
  }

  // ------------------------------------------------------- PLAN COMPARISON

  /*
   * The pricing / plan-comparison grid, rendered from `/api/quotas/tiers`.
   *
   * WHAT IT DELIBERATELY DOES NOT DO:
   *
   * 1. It renders no price unless the tier object carries `price:
   *    {amount_per_period, currency, period}` — the exact shape of
   *    models/core.py:315-332. Today `/tiers` (setup.py:2363-2380) does NOT
   *    serialise that field even though TierConfig holds it, so the price row
   *    is simply absent. The code is forward-compatible; the gap is written up
   *    in the ticket (T-14) rather than papered over with an invented field.
   *
   * 2. It puts a CTA on the CURRENT tier's column only, using nextTier() and
   *    that tier's own `upgrade_url` — the same semantics as the paywall
   *    ("URL to upgrade FROM this tier", models/core.py:444-447). It does NOT
   *    put a "choose this plan" button on every column, because no per-tier
   *    checkout URL exists in the contract and fabricating one would send
   *    customers to a link the consumer never configured (T-15).
   *
   * 3. With no catalog at all it renders NOTHING — not an error, not an empty
   *    shell. Bridge mode does not mount /tiers (setup.py:989) and a pricing
   *    grid with no plans in it is worse than no pricing grid.
   */
  function renderPlans(node, tiers, usage, opts) {
    var area = mountArea(node);
    clear(area);
    if (!tiers || !tiers.length) return;   // silence, not an error state

    var currentId = get(usage, 'tier_id', null);
    var card = el('section', 'ab0t-plans');
    card.setAttribute('aria-label', 'Plan comparison');
    card.appendChild(cardHead(get(opts, 'title', 'Plans'), null));

    // Row labels: /tiers gives resource KEYS only. /usage is the only source
    // of a human name for a resource, so we use it when present and fall back
    // to the raw key rather than prettifying a key into a fake product noun.
    var labels = {};
    (get(usage, 'resources', []) || []).forEach(function (r) {
      if (r.display_name) labels[r.resource_key] = r.display_name;
    });

    // Union of every resource key any tier limits, in first-appearance order.
    var keys = [], seen = {};
    tiers.forEach(function (t) {
      Object.keys(get(t, 'limits', {}) || {}).forEach(function (k) {
        if (!seen[k]) { seen[k] = 1; keys.push(k); }
      });
    });

    var grid = el('div', 'ab0t-plan-grid');
    tiers.forEach(function (t) {
      var isCurrent = (currentId !== null && t.tier_id === currentId);
      var col = el('article', 'ab0t-plan' + (isCurrent ? ' ab0t-plan-current' : ''));
      if (isCurrent) col.setAttribute('aria-current', 'true');

      var nameRow = el('div', 'ab0t-sub-head');
      nameRow.appendChild(el('h4', 'ab0t-plan-name', t.display_name));
      // "Current plan" is a STATE, not a product noun — and it is the
      // non-colour signal that pairs with the accent ring.
      if (isCurrent) nameRow.appendChild(el('span', 'ab0t-plan-chip ab0t-pill-strong', 'Current plan'));
      col.appendChild(nameRow);

      if (t.description) col.appendChild(el('p', 'ab0t-plan-desc', t.description));

      // Price: only if the payload carried BOTH an amount and a code.
      var p = get(t, 'price', null);
      var amt = p ? money(get(p, 'amount_per_period', null), get(p, 'currency', null)) : null;
      if (amt) {
        var pr = el('div', 'ab0t-plan-price', amt);
        var per = get(p, 'period', null);
        if (per) pr.appendChild(el('span', 'ab0t-plan-period', ' / ' + desnake(per)));
        col.appendChild(pr);
      }

      if (keys.length) {
        var ul = el('ul', 'ab0t-plan-limits');
        keys.forEach(function (k) {
          var li = el('li');
          li.appendChild(el('span', 'ab0t-limit-key', labels[k] || k));
          var lim = (get(t, 'limits', {}) || {})[k];
          // limit_display is composed server-side (setup.py:2367) — including
          // the word it uses for "no limit". We do not compose our own.
          li.appendChild(el('span', 'ab0t-limit-val',
            (lim && lim.limit_display !== undefined && lim.limit_display !== null)
              ? String(lim.limit_display) : '—'));
          ul.appendChild(li);
        });
        col.appendChild(ul);
      }

      var feats = tierFeatures(t);
      if (feats) col.appendChild(feats);

      if (isCurrent) {
        var cta = upgradeCta(nextTier(tiers, currentId), get(t, 'upgrade_url', null));
        if (cta) col.appendChild(cta);
      }

      grid.appendChild(col);
    });

    card.appendChild(grid);
    area.appendChild(card);
    announce(node, tiers.length === 1 ? 'One plan available.' : (tiers.length + ' plans available.'));
    emit(node, 'plans-shown', { count: tiers.length, current: currentId });
  }

  function plans(target, opts) {
    var conf = cfg(opts);
    var node = (typeof target === 'string') ? document.querySelector(target) : target;
    if (!node) return null;
    var area = mountArea(node);

    function load() {
      skeleton(area, 2);
      Promise.all([
        // No catalog is a first-class state, not a failure.
        api(conf, '/tiers').catch(function () { return null; }),
        // Anonymous pricing pages have no session; a 401 here just means
        // "no current tier", which the grid handles by marking none.
        api(conf, '/usage').catch(function () { return null; })
      ]).then(function (r) {
        renderPlans(node, r[0] ? get(r[0], 'tiers', null) : null, r[1], opts);
      });
    }

    load();
    node.ab0tRefresh = load;
    return node;
  }

  // ------------------------------------------------------------- INVOICES

  /*
   * `GET {billingBase}/payments/invoices` -> InvoicesResponse
   * (billing/models.py:266). Every money figure here carries its own
   * `currency` field on the SAME object (models.py:259), so money() has what
   * it needs; an invoice that omits the code renders its dates and status but
   * no amount, which is the correct failure.
   *
   * `status` is UPSTREAM vocabulary (Stripe's, via payment). We render it
   * verbatim, de-snaked, and never branch on its value — a client that
   * hardcodes the set of known statuses breaks the day the provider adds one.
   * The one signal we do derive is arithmetic, not vocabulary: amount_due > 0.
   */
  function renderInvoices(node, data) {
    var area = mountArea(node);
    clear(area);

    var list = get(data, 'invoices', []) || [];
    var card = el('section', 'ab0t-invoices');
    card.setAttribute('aria-label', 'Invoices');
    card.appendChild(cardHead('Invoices', null));

    if (!list.length) {
      var body = el('div');
      emptyState(body, 'inbox', 'No invoices yet.');
      card.appendChild(body.firstChild);
      area.appendChild(card);
      announce(node, 'No invoices yet.');
      return;
    }

    var rows = el('div', 'ab0t-rows');
    list.forEach(function (inv) {
      var due = parseFloat(String(get(inv, 'amount_due', '0')));
      var outstanding = isFinite(due) && due > 0;

      var row = el('div', 'ab0t-row' + (outstanding ? ' ab0t-row-due' : ''));
      var main = el('div', 'ab0t-row-main');
      main.appendChild(el('span', 'ab0t-row-title',
        get(inv, 'invoice_number', null) || get(inv, 'invoice_id', '')));

      var meta = [];
      var created = date(get(inv, 'created_at', null));
      if (created) meta.push(created);
      var dd = date(get(inv, 'due_date', null));
      if (dd) meta.push('due ' + dd);
      if (meta.length) main.appendChild(el('span', 'ab0t-row-meta', meta.join(' · ')));
      row.appendChild(main);

      var side = el('div', 'ab0t-row-side');
      var st = get(inv, 'status', null);
      if (st) side.appendChild(el('span', 'ab0t-pill', desnake(st)));
      if (outstanding) {
        var p = el('span', 'ab0t-pill');
        p.appendChild(icon('warn'));
        p.appendChild(el('span', null, 'Amount due'));
        side.appendChild(p);
      }
      var amt = money(get(inv, 'total_amount', null), get(inv, 'currency', null));
      if (amt) side.appendChild(el('span', 'ab0t-amount', amt));

      var pdf = safeUrl(get(inv, 'pdf_url', null));
      if (pdf) {
        var a = el('a', 'ab0t-file');
        a.setAttribute('href', pdf);
        a.setAttribute('rel', 'noopener');
        a.appendChild(icon('download'));
        a.appendChild(el('span', null, 'PDF'));
        side.appendChild(a);
      }
      row.appendChild(side);
      rows.appendChild(row);
    });
    card.appendChild(rows);

    if (get(data, 'has_more', false)) {
      card.appendChild(el('p', 'ab0t-more', 'Showing the most recent ' + num(list.length) + '.'));
    }

    area.appendChild(card);
    announce(node, num(list.length) + ' invoices listed.');
    emit(node, 'invoices-shown', { count: list.length });
  }

  function invoices(target, opts) {
    var conf = cfg(opts);
    var node = (typeof target === 'string') ? document.querySelector(target) : target;
    if (!node) return null;
    var area = mountArea(node);

    function load() {
      skeleton(area, 3);
      api(conf, '/payments/invoices', conf.billingBase)
        .then(function (d) { renderInvoices(node, d); })
        .catch(function (e) {
          if (e && e.code === 'auth') { clear(area); emit(node, 'auth-required', {}); return; }
          quietError(area, load);
        });
    }

    load();
    node.ab0tRefresh = load;
    return node;
  }

  // ------------------------------------------------------ BALANCE / SPEND

  /*
   * Two endpoints, two very different contracts:
   *
   *   {billingBase}/billing/balance        BillingBalanceResponse — carries
   *                                        `currency` (models.py:22). Money
   *                                        renders.
   *   {billingBase}/billing/usage/summary  BillingUsageSummaryResponse —
   *                                        carries `total_cost` but NO
   *                                        currency field (models.py:26-42).
   *
   * We do NOT borrow the balance's code for the spend figure. They are
   * different services (billing 8002 vs the org's ledger) and an amount shown
   * in the wrong currency is a worse defect than an amount not shown. If the
   * summary response carries a code via extra="allow", we use it; otherwise
   * the spend tile shows the period and says the amount is unavailable.
   * Written up as T-16.
   */
  function renderBalance(node, bal, summary) {
    var area = mountArea(node);
    clear(area);

    var card = el('section', 'ab0t-balance');
    card.setAttribute('aria-label', 'Balance and spend');
    card.appendChild(cardHead('Balance', null));

    var figs = el('div', 'ab0t-figures');
    var code = get(bal, 'currency', null);

    function figure(label, value, note) {
      var f = el('div', 'ab0t-figure');
      f.appendChild(el('span', 'ab0t-figure-label', label));
      f.appendChild(el('span', 'ab0t-figure-value', value));
      if (note) f.appendChild(el('span', 'ab0t-figure-note', note));
      return f;
    }

    var b = money(get(bal, 'balance', null), code);
    var av = money(get(bal, 'available_balance', null), code);
    if (b) figs.appendChild(figure('Balance', b));
    if (av) figs.appendChild(figure('Available', av, 'After reservations'));

    if (summary) {
      var period = [date(get(summary, 'period_start', null)), date(get(summary, 'period_end', null))]
        .filter(Boolean).join(' – ');
      // The summary object's OWN code only. Never the balance's.
      var sCode = get(summary, 'currency', null) ||
                  get(get(summary, 'summary', {}) || {}, 'currency', null);
      var spend = money(get(summary, 'total_cost', null), sCode);
      figs.appendChild(spend
        ? figure('Spend this period', spend, period || null)
        : figure('Spend this period', '—',
            period ? (period + ' · amount unavailable') : 'Amount unavailable'));
    }

    if (!figs.childNodes.length) {
      emptyState(area, 'wallet', 'No balance information available.');
      announce(node, 'No balance information available.');
      return;
    }

    card.appendChild(figs);
    area.appendChild(card);
    announce(node, 'Balance updated.');
    emit(node, 'balance-shown', {});
  }

  function balance(target, opts) {
    var conf = cfg(opts);
    var node = (typeof target === 'string') ? document.querySelector(target) : target;
    if (!node) return null;
    var area = mountArea(node);

    function load() {
      skeleton(area, 2);
      Promise.all([
        api(conf, '/billing/balance', conf.billingBase),
        // The spend summary is an enrichment: a balance is still worth
        // showing when the usage roll-up is down.
        api(conf, '/billing/usage/summary', conf.billingBase).catch(function () { return null; })
      ]).then(function (r) { renderBalance(node, r[0], r[1]); })
        .catch(function (e) {
          if (e && e.code === 'auth') { clear(area); emit(node, 'auth-required', {}); return; }
          quietError(area, load);
        });
    }

    load();
    node.ab0tRefresh = load;
    return node;
  }

  // --------------------------------------------------------- SUBSCRIPTION

  /*
   * `GET {billingBase}/payments/subscriptions` -> SubscriptionsResponse
   * (billing/models.py:231).
   *
   * Two contract gaps this component refuses to paper over:
   *   - SubscriptionItem has `amount` but NO currency field
   *     (models.py:217). The amount therefore renders only when the payload
   *     supplies a code via extra="allow". Written up as T-16.
   *   - The item identifies its plan by `plan_id` only — there is no
   *     display_name on the contract. We print the id VERBATIM. Title-casing
   *     an id into a plausible product name is exactly the defect
   *     TICKET_config_is_king §5e removed from billing.
   */
  function renderSubscription(node, data) {
    var area = mountArea(node);
    clear(area);

    var subs = get(data, 'subscriptions', []) || [];
    var card = el('section', 'ab0t-subscription');
    card.setAttribute('aria-label', 'Subscription');
    card.appendChild(cardHead('Subscription', null));

    if (!subs.length) {
      var holder = el('div');
      emptyState(holder, 'receipt', 'No active subscription.');
      card.appendChild(holder.firstChild);
      area.appendChild(card);
      announce(node, 'No active subscription.');
      return;
    }

    subs.forEach(function (s) {
      var box = el('div', 'ab0t-sub');

      var head = el('div', 'ab0t-sub-head');
      var planId = get(s, 'plan_id', null);
      if (planId) head.appendChild(el('span', 'ab0t-sub-plan', planId));
      var st = get(s, 'status', null);
      if (st) head.appendChild(el('span', 'ab0t-plan-chip ab0t-pill-strong', desnake(st)));
      box.appendChild(head);

      var dl = el('dl', 'ab0t-facts');
      function fact(k, v) {
        if (!v) return;
        dl.appendChild(el('dt', null, k));
        dl.appendChild(el('dd', null, v));
      }
      var amt = money(get(s, 'amount', null), get(s, 'currency', null));
      fact('Amount', amt);
      fact('Current period ends', date(get(s, 'current_period_end', null)));
      fact('Next billing date', date(get(s, 'next_billing_date', null)));
      fact('Trial ends', date(get(s, 'trial_end', null)));
      if (dl.childNodes.length) box.appendChild(dl);

      if (get(s, 'cancel_at_period_end', false)) {
        var n = el('div', 'ab0t-notice');
        n.appendChild(icon('calendar'));
        var end = date(get(s, 'current_period_end', null));
        n.appendChild(el('span', null, end
          ? ('This subscription ends on ' + end + ' and will not renew.')
          : 'This subscription will not renew.'));
        box.appendChild(n);
      }

      card.appendChild(box);
    });

    area.appendChild(card);
    announce(node, num(subs.length) + ' subscriptions listed.');
    emit(node, 'subscription-shown', { count: subs.length });
  }

  function subscription(target, opts) {
    var conf = cfg(opts);
    var node = (typeof target === 'string') ? document.querySelector(target) : target;
    if (!node) return null;
    var area = mountArea(node);

    function load() {
      skeleton(area, 2);
      api(conf, '/payments/subscriptions', conf.billingBase)
        .then(function (d) { renderSubscription(node, d); })
        .catch(function (e) {
          if (e && e.code === 'auth') { clear(area); emit(node, 'auth-required', {}); return; }
          quietError(area, load);
        });
    }

    load();
    node.ab0tRefresh = load;
    return node;
  }

  // ------------------------------------------------------------ auto-mount

  var MOUNTERS = {
    usage: usage, warning: usage, plans: plans,
    invoices: invoices, balance: balance, subscription: subscription
  };

  function autoMount(root) {
    (root || document).querySelectorAll('[data-ab0t]').forEach(function (n) {
      if (n.__ab0tMounted) return;
      n.__ab0tMounted = true;
      var kind = n.getAttribute('data-ab0t');
      var opts = {
        base: n.getAttribute('data-ab0t-base') || undefined,
        billingBase: n.getAttribute('data-ab0t-billing-base') || undefined,
        title: n.getAttribute('data-ab0t-title') || undefined
      };
      var fn = MOUNTERS[kind];
      if (fn) fn(n, opts);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { autoMount(); });
  } else {
    autoMount();
  }

  global.AB0TQuotaUI = {
    version: '0.2.0',
    usage: usage,
    paywall: paywall,
    plans: plans,
    invoices: invoices,
    balance: balance,
    subscription: subscription,
    autoMount: autoMount,
    // exported for tests / demo harness
    _internal: {
      nextTier: nextTier, meter: meter, renderUsage: renderUsage,
      renderPlans: renderPlans, renderInvoices: renderInvoices,
      renderBalance: renderBalance, renderSubscription: renderSubscription,
      safeUrl: safeUrl, money: money, date: date, desnake: desnake, icon: icon
    }
  };

})(typeof window !== 'undefined' ? window : this);
