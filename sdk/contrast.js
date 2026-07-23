/*
 * contrast.js — WCAG 2.1 contrast gate for ab0t-quota-ui.css.
 *
 * This does not read a palette written in a comment. It parses the SHIPPED
 * stylesheet, extracts the semantic colour tokens for each theme, and computes
 * the relative-luminance ratio for every pair the components actually put on
 * top of each other. Required by `test_sdk.js` gates X4 (base themes) and X5 (white-label brands);
 * also runnable alone:
 *
 *     node sdk/contrast.js          # prints the full table
 *
 * Thresholds (WCAG 2.1):
 *   4.5:1  normal body text          (1.4.3 AA)
 *   3.0:1  large text >= 18.66px bold / 24px, and non-text UI (1.4.11 AA)
 */
'use strict';

var fs = require('fs');
var path = require('path');

// ------------------------------------------------------------------ colour

function hex(c) {
  var h = c.trim().replace(/^#/, '');
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  if (!/^[0-9a-fA-F]{6}$/.test(h)) return null;
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

function luminance(rgb) {
  var a = rgb.map(function (v) {
    var s = v / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2];
}

function ratio(fg, bg) {
  var l1 = luminance(fg), l2 = luminance(bg);
  var hi = Math.max(l1, l2), lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

// ------------------------------------------------------------- CSS parsing

/* Pull `--ab0t-x: #rrggbb;` declarations out of a { ... } body. */
function decls(body) {
  var out = {}, re = /(--ab0t-[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;/g, m;
  while ((m = re.exec(body))) out[m[1]] = m[2];
  return out;
}

/* Find the body of the first block whose selector line matches `sel`, starting
 * the search at `from`. Brace-counted, so nested blocks are handled. */
function blockAfter(css, sel, from) {
  var i = css.indexOf(sel, from || 0);
  if (i < 0) return null;
  var open = css.indexOf('{', i);
  if (open < 0) return null;
  var depth = 0;
  for (var j = open; j < css.length; j++) {
    if (css[j] === '{') depth++;
    else if (css[j] === '}') { depth--; if (!depth) return { body: css.slice(open + 1, j), end: j }; }
  }
  return null;
}

function themes(cssPath) {
  var css = fs.readFileSync(cssPath, 'utf8');

  var light = decls(blockAfter(css, ':root[data-theme="light"]').body);
  var dark = decls(blockAfter(css, ':root[data-theme="dark"]').body);

  // Drift guard: the media-query / bare-:root declarations must agree with the
  // explicit [data-theme] blocks, or a viewer toggling the theme gets a
  // different palette from the one this gate measured.
  var mq = blockAfter(css, '@media (prefers-color-scheme: dark)');
  var mqDark = decls(blockAfter(mq.body, ':root').body);

  // All bare `:root {` blocks before the dark media query, merged.
  var bare = {}, at = 0, b;
  var cut = css.indexOf('@media (prefers-color-scheme: dark)');
  while ((b = blockAfter(css, ':root {', at)) && b.end < cut) {
    Object.assign(bare, decls(b.body));
    at = b.end;
  }

  return { light: light, dark: dark, defaultLight: bare, mediaDark: mqDark };
}

// ------------------------------------------------------- the pairs we ship
// Each entry is [foreground token, background token, minimum, what renders it].

var TEXT = [
  ['--ab0t-fg', '--ab0t-bg', 4.5, 'body text on a page background'],
  ['--ab0t-fg', '--ab0t-bg-raised', 4.5, 'body text on a card'],
  ['--ab0t-fg', '--ab0t-bg-sunken', 4.5, 'body text on a sunken figure tile'],
  ['--ab0t-fg-muted', '--ab0t-bg', 4.5, 'meter value / meta on a page'],
  ['--ab0t-fg-muted', '--ab0t-bg-raised', 4.5, 'meter value / meta on a card'],
  ['--ab0t-fg-muted', '--ab0t-bg-sunken', 4.5, 'plan chip, figure label'],
  ['--ab0t-accent-fg', '--ab0t-accent', 4.5, 'CTA label, resting'],
  ['--ab0t-accent-fg', '--ab0t-accent-hover', 4.5, 'CTA label, hover'],
  ['--ab0t-accent-fg', '--ab0t-accent-active', 4.5, 'CTA label, active'],
  ['--ab0t-accent', '--ab0t-bg', 4.5, 'link / retry button'],
  ['--ab0t-accent', '--ab0t-bg-raised', 4.5, 'invoice PDF link on a card'],
  ['--ab0t-accent', '--ab0t-bg-sunken', 4.5, 'link on a sunken tile'],
  ['--ab0t-info', '--ab0t-bg-raised', 4.5, 'OK badge'],
  ['--ab0t-warning', '--ab0t-bg-raised', 4.5, 'Approaching badge'],
  ['--ab0t-critical', '--ab0t-bg-raised', 4.5, 'Nearly-at-limit badge'],
  ['--ab0t-exceeded', '--ab0t-bg-raised', 4.5, 'Limit-reached badge'],
  ['--ab0t-info', '--ab0t-bg-sunken', 4.5, 'OK badge on a sunken tile'],
  ['--ab0t-warning', '--ab0t-bg-sunken', 4.5, 'Approaching badge on a sunken tile'],
  ['--ab0t-critical', '--ab0t-bg-sunken', 4.5, 'Nearly badge on a sunken tile'],
  ['--ab0t-exceeded', '--ab0t-bg-sunken', 4.5, 'Exceeded badge on a sunken tile'],
  ['--ab0t-warning', '--ab0t-warning-bg', 4.5, 'warning icon on the warning banner'],
  ['--ab0t-critical', '--ab0t-critical-bg', 4.5, 'critical icon on the critical banner'],
  ['--ab0t-exceeded', '--ab0t-exceeded-bg', 4.5, 'exceeded icon on its tint'],
  ['--ab0t-info', '--ab0t-info-bg', 4.5, 'ok icon on its tint'],
  ['--ab0t-fg', '--ab0t-warning-bg', 4.5, 'banner body text'],
  ['--ab0t-fg', '--ab0t-critical-bg', 4.5, 'critical banner body text'],
  ['--ab0t-fg-muted', '--ab0t-warning-bg', 4.5, 'banner meta text'],
  ['--ab0t-fg-muted', '--ab0t-critical-bg', 4.5, 'critical banner meta text'],
  ['--ab0t-critical', '--ab0t-bg-raised', 4.5, 'amount-due pill'],
];

var NONTEXT = [
  ['--ab0t-focus', '--ab0t-bg', 3.0, 'focus ring against a page'],
  ['--ab0t-focus', '--ab0t-bg-raised', 3.0, 'focus ring against a card'],
  ['--ab0t-focus', '--ab0t-bg-sunken', 3.0, 'focus ring against a tile'],
  ['--ab0t-accent', '--ab0t-bg-raised', 3.0, 'current-plan ring'],
  ['--ab0t-info', '--ab0t-bg-sunken', 3.0, 'meter fill vs track (ok)'],
  ['--ab0t-warning', '--ab0t-bg-sunken', 3.0, 'meter fill vs track (approaching)'],
  ['--ab0t-critical', '--ab0t-bg-sunken', 3.0, 'meter fill vs track (nearly)'],
  ['--ab0t-exceeded', '--ab0t-bg-sunken', 3.0, 'meter fill vs track (reached)'],
  ['--ab0t-border-strong', '--ab0t-bg-raised', 3.0, 'secondary-button boundary / override pill outline'],
  ['--ab0t-border-strong', '--ab0t-bg-sunken', 3.0, 'secondary-button boundary on a tile'],
];

// --------------------------------------------------------------- the check

function check(cssPath) {
  var t = themes(cssPath || path.join(__dirname, 'ab0t-quota-ui.css'));
  var rows = [], failures = [];

  ['light', 'dark'].forEach(function (name) {
    var tok = t[name];
    TEXT.concat(NONTEXT).forEach(function (p) {
      var fg = hex(tok[p[0]] || ''), bg = hex(tok[p[1]] || '');
      if (!fg || !bg) {
        failures.push(name + ': missing token ' + (fg ? p[1] : p[0]));
        return;
      }
      var r = ratio(fg, bg);
      var ok = r >= p[2] - 1e-9;
      rows.push({ theme: name, fg: p[0], bg: p[1], ratio: r, min: p[2], ok: ok, what: p[3] });
      if (!ok) {
        failures.push(name + ': ' + p[0] + ' on ' + p[1] + ' = ' +
          r.toFixed(2) + ':1, needs ' + p[2].toFixed(1) + ':1 (' + p[3] + ')');
      }
    });
  });

  // Drift guard — the toggle palette must equal the media-query palette.
  [['defaultLight', 'light'], ['mediaDark', 'dark']].forEach(function (pair) {
    var a = t[pair[0]], b = t[pair[1]];
    Object.keys(b).forEach(function (k) {
      if (a[k] && a[k].toLowerCase() !== b[k].toLowerCase()) {
        failures.push('theme drift: ' + k + ' is ' + a[k] + ' in ' + pair[0] +
                      ' but ' + b[k] + ' in :root[data-theme="' + pair[1] + '"]');
      }
    });
  });

  return { rows: rows, failures: failures };
}

/*
 * A white-label brand overrides SOME tokens on a wrapper and INHERITS the
 * rest from :root — so a brand that changes only its surfaces can silently
 * break the severity colours it did not touch. This merges the base theme
 * with the brand's overrides and runs the identical pair list over the
 * result, which is the only way to know the branded widgets are still AA.
 */
function checkBrand(brandCssPath, baseCssPath, lightSel, darkSel) {
  var brandCss = fs.readFileSync(brandCssPath, 'utf8');
  var base = themes(baseCssPath || path.join(__dirname, 'ab0t-quota-ui.css'));

  function collect(sel) {
    var out = {}, at = 0, b;
    while ((b = blockAfter(brandCss, sel, at))) {
      Object.assign(out, decls(b.body));
      at = b.end;
    }
    return out;
  }

  var merged = {
    light: Object.assign({}, base.light, collect(lightSel)),
    dark: Object.assign({}, base.dark, collect(darkSel))
  };

  var rows = [], failures = [];
  ['light', 'dark'].forEach(function (name) {
    var tok = merged[name];
    TEXT.concat(NONTEXT).forEach(function (p) {
      var fg = hex(tok[p[0]] || ''), bg = hex(tok[p[1]] || '');
      if (!fg || !bg) return;
      var r = ratio(fg, bg);
      var ok = r >= p[2] - 1e-9;
      rows.push({ theme: name, fg: p[0], bg: p[1], ratio: r, min: p[2], ok: ok, what: p[3] });
      if (!ok) {
        failures.push(lightSel + ' / ' + name + ': ' + p[0] + ' on ' + p[1] + ' = ' +
          r.toFixed(2) + ':1, needs ' + p[2].toFixed(1) + ':1 (' + p[3] + ')');
      }
    });
  });
  return { rows: rows, failures: failures };
}

module.exports = { check: check, checkBrand: checkBrand, ratio: ratio, hex: hex };

if (require.main === module) {
  var r = check();
  var w = 0;
  r.rows.forEach(function (x) { w = Math.max(w, (x.fg + ' on ' + x.bg).length); });
  var theme = '';
  r.rows.forEach(function (x) {
    if (x.theme !== theme) { theme = x.theme; console.log('\n  == ' + theme.toUpperCase() + ' ==='); }
    var pair = x.fg + ' on ' + x.bg;
    console.log('  ' + (x.ok ? 'ok  ' : 'FAIL') + '  ' +
      pair + ' '.repeat(w - pair.length) + '  ' +
      x.ratio.toFixed(2).padStart(6) + ':1  (min ' + x.min.toFixed(1) + ')  ' + x.what);
  });
  console.log('\n  ' + (r.failures.length ? r.failures.length + ' FAILURES' : 'all pairs pass') + '\n');
  r.failures.forEach(function (f) { console.log('    - ' + f); });
  process.exit(r.failures.length ? 1 : 0);
}
