"""Render events into one self-contained HTML file.

Self-contained means it opens from a thumb drive with the network unplugged: no CDN, no
webfont, no external stylesheet, no fetch. A bug report that needs a working internet
connection to read is not much of an attachment.

Everything the page shows is escaped before it is inserted, and the event data travels as JSON
in a script block rather than as markup, so a transcript containing HTML cannot restyle or
script the page that displays it. Transcripts are full of HTML, since agents read and write web
files all day, which makes this a routine case rather than an adversarial one.
"""
from __future__ import annotations

import html
import json

from .cost import RATE_SOURCE, accumulate, running

CSS = """
:root{
  --bg:#f6f7f9; --panel:#fff; --ink:#12161b; --ink2:#4a5561; --ink3:#79838f;
  --line:#dee2e7; --rule:#eef0f3;
  --user:#1c5fb8; --user-bg:#e9f1fd; --asst:#2f2a5e; --asst-bg:#eeecf8;
  --tool:#5a4a12; --tool-bg:#faf3dd; --err:#9a2620; --err-bg:#fceceb;
  --ok:#1a6340; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --add-bg:#e4f5e9; --add-ink:#12572f; --del-bg:#fdeae9; --del-ink:#8c221c;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0f1216; --panel:#171b21; --ink:#e7ebef; --ink2:#a3adb8; --ink3:#727d89;
  --line:#282f37; --rule:#1f252b;
  --user:#8ab4f8; --user-bg:#16233a; --asst:#b6aef5; --asst-bg:#221f3a;
  --tool:#e0bd70; --tool-bg:#2b2415; --err:#f0918a; --err-bg:#331a19;
  --ok:#6cc494; --add-bg:#12301f; --add-ink:#7ddb9f; --del-bg:#341a19; --del-ink:#f0918a;
}}
:root[data-theme=dark]{
  --bg:#0f1216; --panel:#171b21; --ink:#e7ebef; --ink2:#a3adb8; --ink3:#727d89;
  --line:#282f37; --rule:#1f252b;
  --user:#8ab4f8; --user-bg:#16233a; --asst:#b6aef5; --asst-bg:#221f3a;
  --tool:#e0bd70; --tool-bg:#2b2415; --err:#f0918a; --err-bg:#331a19;
  --ok:#6cc494; --add-bg:#12301f; --add-ink:#7ddb9f; --del-bg:#341a19; --del-ink:#f0918a;
}
:root[data-theme=light]{
  --bg:#f6f7f9; --panel:#fff; --ink:#12161b; --ink2:#4a5561; --ink3:#79838f;
  --line:#dee2e7; --rule:#eef0f3;
  --user:#1c5fb8; --user-bg:#e9f1fd; --asst:#2f2a5e; --asst-bg:#eeecf8;
  --tool:#5a4a12; --tool-bg:#faf3dd; --err:#9a2620; --err-bg:#fceceb;
  --ok:#1a6340; --add-bg:#e4f5e9; --add-ink:#12572f; --del-bg:#fdeae9; --del-ink:#8c221c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:0 1rem}

header{background:var(--panel);border-bottom:1px solid var(--line);padding:1.5rem 0 0}
h1{font-size:1.35rem;margin:0 0 .3rem;letter-spacing:-.01em}
.sub{color:var(--ink2);font-size:.85rem;margin:0 0 1rem}
.stats{display:flex;gap:1.75rem;flex-wrap:wrap;margin-bottom:1rem}
.stat{display:flex;flex-direction:column}
.stat b{font:700 1.15rem/1.1 var(--mono);font-variant-numeric:tabular-nums}
.stat span{font-size:.7rem;color:var(--ink3);text-transform:uppercase;letter-spacing:.07em;
  margin-top:.2rem}
.banner{border-left:3px solid var(--tool);background:var(--tool-bg);color:var(--ink2);
  padding:.6rem .8rem;margin:0 0 1rem;font-size:.82rem;border-radius:0 3px 3px 0}
.banner b{color:var(--ink)}
.banner.safe{border-left-color:var(--ok);background:var(--panel);border:1px solid var(--line);
  border-left:3px solid var(--ok)}

.controls{position:sticky;top:0;z-index:20;background:var(--panel);
  border-bottom:1px solid var(--line);padding:.6rem 0}
.row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
input[type=search],select,button{font:inherit;font-size:.82rem;padding:.35rem .55rem;
  border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--ink);
  max-width:100%;min-width:0}
button{cursor:pointer;background:var(--panel)}
button:hover{border-color:var(--user);color:var(--user)}
:focus-visible{outline:2px solid var(--user);outline-offset:1px}
#q{flex:1 1 14rem}
#scrub{flex:1 1 100%;margin:.55rem 0 0;accent-color:var(--user);width:100%}
.pos{font:.75rem/1 var(--mono);color:var(--ink3);font-variant-numeric:tabular-nums;
  white-space:nowrap}

main{padding:1rem 0 4rem}
.ev{display:grid;grid-template-columns:7.5rem 1fr;gap:.75rem;padding:.5rem 0;
  border-bottom:1px solid var(--rule);scroll-margin-top:6rem}
.ev.hit{background:var(--user-bg);border-radius:4px}
.gutter{font:.7rem/1.45 var(--mono);color:var(--ink3);text-align:right;
  font-variant-numeric:tabular-nums;padding-top:.15rem}
.gutter .money{color:var(--ink2);font-weight:700}
.body{min-width:0}
.head{display:flex;gap:.5rem;align-items:baseline;flex-wrap:wrap}
.who{font-size:.68rem;text-transform:uppercase;letter-spacing:.08em;font-weight:800;
  padding:.1rem .35rem;border-radius:3px}
.user .who{color:var(--user);background:var(--user-bg)}
.assistant .who{color:var(--asst);background:var(--asst-bg)}
.tool_use .who,.tool_result .who{color:var(--tool);background:var(--tool-bg)}
.thinking .who{color:var(--ink3);background:var(--rule)}
.system .who,.unknown .who{color:var(--ink3);background:var(--rule)}
.err .who{color:var(--err);background:var(--err-bg)}
/* MCP tool names are long and unbroken, for example
   mcp__plugin_playwright_playwright__browser_navigate at 50 characters, which pushed 106
   elements past a 390px viewport. Measured in a browser; nothing about the markup suggested
   it. */
.tname{font:.78rem var(--mono);font-weight:700;overflow-wrap:anywhere;min-width:0}
.head{min-width:0}
.text{white-space:pre-wrap;overflow-wrap:anywhere;margin:.3rem 0 0;font-size:.87rem}
details{margin:.3rem 0 0}
summary{cursor:pointer;font-size:.78rem;color:var(--ink2);font-family:var(--mono)}
summary:hover{color:var(--user)}
pre{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:.55rem .7rem;
  overflow-x:auto;font:.78rem/1.5 var(--mono);margin:.35rem 0;max-height:26rem}
.diff{border:1px solid var(--line);border-radius:4px;overflow:hidden;margin:.35rem 0}
.diff div{font:.78rem/1.5 var(--mono);padding:.1rem .55rem;white-space:pre-wrap;
  overflow-wrap:anywhere}
.diff .d{background:var(--del-bg);color:var(--del-ink)}
.diff .a{background:var(--add-bg);color:var(--add-ink)}
.redacted{background:var(--tool-bg);color:var(--tool);font-weight:700;padding:0 .2em;
  border-radius:2px;font-family:var(--mono);font-size:.92em}
mark{background:var(--user-bg);color:inherit;border-radius:2px}
.trunc{color:var(--ink3);font-size:.75rem;font-style:italic}
footer{border-top:1px solid var(--line);padding:1.25rem 0 3rem;color:var(--ink3);font-size:.78rem}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
@media (max-width:640px){.ev{grid-template-columns:1fr;gap:.2rem}
  .gutter{text-align:left}}
"""

JS = r"""
(function () {
  var D = JSON.parse(document.getElementById('events').textContent);
  var list = document.getElementById('list');
  var q = document.getElementById('q'), kind = document.getElementById('kind');
  var tool = document.getElementById('tool'), scrub = document.getElementById('scrub');
  var pos = document.getElementById('pos'), only = document.getElementById('onlyerr');
  var visible = [];

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  // Placeholders are highlighted so a reader can see WHERE something was removed. Applied
  // after escaping, so the markup here is the only markup that can reach the DOM.
  function mark(s, terms) {
    var out = esc(s);
    (terms || []).forEach(function (t) {
      if (t.length < 2) return;
      out = out.replace(new RegExp('(' + t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi'),
                        '<mark>$1</mark>');
    });
    return out.replace(/\[redacted:([a-z\-]+)\]/g,
                       '<span class="redacted" title="a secret was removed here">[$1]</span>');
  }
  function money(v) { return '$' + (v < 0.01 ? v.toFixed(4) : v.toFixed(2)); }
  function toks(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(n);
  }

  function block(e, terms) {
    var h = '';
    if (e.diff) {
      h += '<div class="diff">';
      e.diff.forEach(function (d) {
        h += '<div class="' + (d[0] === '+' ? 'a' : 'd') + '">' + esc(d) + '</div>';
      });
      h += '</div>';
    }
    if (e.args) {
      h += '<details><summary>arguments</summary><pre>' + mark(e.args, terms) + '</pre></details>';
    }
    if (e.text) {
      if (e.kind === 'tool_result' || e.kind === 'tool_use') {
        h += '<details' + (e.err ? ' open' : '') + '><summary>' +
             (e.err ? 'error output' : 'output') + ' (' + e.len + ' chars)</summary><pre>' +
             mark(e.text, terms) + '</pre></details>';
      } else {
        h += '<p class="text">' + mark(e.text, terms) + '</p>';
      }
    }
    if (e.trunc) h += '<p class="trunc">output truncated, ' + e.trunc + ' more characters</p>';
    return h;
  }

  function render() {
    var terms = q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    var k = kind.value, t = tool.value, e404 = only.checked;
    visible = D.filter(function (e) {
      if (k && e.kind !== k) return false;
      if (t && e.tool !== t) return false;
      if (e404 && !e.err) return false;
      if (terms.length) {
        var hay = (e.text + ' ' + (e.tool || '') + ' ' + (e.args || '')).toLowerCase();
        for (var i = 0; i < terms.length; i++) if (hay.indexOf(terms[i]) === -1) return false;
      }
      return true;
    });
    list.innerHTML = visible.map(function (e) {
      return '<article class="ev ' + e.kind + (e.err ? ' err' : '') + '" id="e' + e.i + '">' +
        '<div class="gutter">' + esc(e.clock) + '<br><span class="money">' + (e.usd === null ? 'not supplied' : money(e.usd)) +
        '</span><br>' + (e.tok === null ? '' : toks(e.tok)) + '</div>' +
        '<div class="body"><div class="head"><span class="who">' +
        esc(e.kind.replace('_', ' ')) + '</span>' +
        (e.tool ? '<span class="tname">' + esc(e.tool) + '</span>' : '') +
        '</div>' + block(e, terms) + '</div></article>';
    }).join('');
    scrub.max = Math.max(0, visible.length - 1);
    scrub.value = 0;
    updatePos();
  }
  function updatePos() {
    pos.textContent = (visible.length ? (+scrub.value + 1) : 0) + ' / ' + visible.length +
      ' of ' + D.length;
  }
  scrub.addEventListener('input', function () {
    var e = visible[+scrub.value];
    if (!e) return;
    var el = document.getElementById('e' + e.i);
    if (el) el.scrollIntoView({ block: 'center' });
    updatePos();
  });
  [q, kind, tool, only].forEach(function (el) { el.addEventListener('input', render); });
  document.getElementById('expand').addEventListener('click', function () {
    document.querySelectorAll('details').forEach(function (d) { d.open = true; });
  });
  document.getElementById('collapse').addEventListener('click', function () {
    document.querySelectorAll('details').forEach(function (d) { d.open = false; });
  });
  var themes = ['auto', 'light', 'dark'], ti = 0;
  document.getElementById('theme').addEventListener('click', function () {
    ti = (ti + 1) % 3;
    if (themes[ti] === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', themes[ti]);
    this.textContent = 'Theme: ' + themes[ti];
  });
  render();
})();
"""


def _diff_lines(old: str, new: str, limit: int = 40):
    """A minimal line diff for Edit calls, enough to see what changed.

    Not a real LCS. Equal leading and trailing lines are dropped and the differing middle is
    shown whole, which is correct for the common single-hunk edit and honest about the rest,
    since it never claims a line is unchanged when it is not.
    """
    o = (old or "").split("\n")
    n = (new or "").split("\n")
    head = 0
    while head < len(o) and head < len(n) and o[head] == n[head]:
        head += 1
    tail = 0
    while (tail < len(o) - head and tail < len(n) - head
           and o[len(o) - 1 - tail] == n[len(n) - 1 - tail]):
        tail += 1
    om = o[head:len(o) - tail]
    nm = n[head:len(n) - tail]
    out = [f"-{l}" for l in om[:limit]] + [f"+{l}" for l in nm[:limit]]
    if len(om) > limit or len(nm) > limit:
        out.append(f" … {max(len(om), len(nm)) - limit} more changed lines")
    return out


def build(events, meta: dict, *, max_output: int = 6000) -> str:
    gutter = running(events)
    total = accumulate(events)

    payload = []
    for e, (usd, tok) in zip(events, gutter):
        if e.hidden:
            continue
        text = e.text or ""
        trunc = 0
        if len(text) > max_output:
            trunc = len(text) - max_output
            text = text[:max_output]
        args = ""
        diff = None
        if e.kind == "tool_use" and e.tool_input:
            ti = dict(e.tool_input)
            if e.tool in ("Edit", "NotebookEdit") and "old_string" in ti:
                diff = _diff_lines(ti.pop("old_string", ""), ti.pop("new_string", ""))
            if e.tool == "Write" and "content" in ti:
                c = ti.pop("content", "")
                diff = [f"+{l}" for l in c.split("\n")[:40]]
                if c.count("\n") > 40:
                    diff.append(f" … {c.count(chr(10)) - 40} more lines")
            args = json.dumps(ti, indent=1)[:max_output] if ti else ""
        payload.append({
            "i": e.index, "kind": e.kind, "tool": e.tool, "text": text,
            "len": len(e.text or ""), "trunc": trunc, "err": e.is_error,
            "args": args, "diff": diff, "usd": round(usd, 6), "tok": tok,
            "clock": (e.ts or "")[11:19],
        })

    # A trailing usage-only snapshot has no safe content to render, but the scrubber's final
    # visible row must still agree with the header and aggregate totals.
    if payload and gutter:
        payload[-1]["usd"] = round(gutter[-1][0], 6)
        payload[-1]["tok"] = gutter[-1][1]

    if not meta.get("accounting_available", True):
        for event in payload:
            event["usd"] = event["tok"] = None

    kinds = sorted({e["kind"] for e in payload})
    tools = sorted({e["tool"] for e in payload if e["tool"]})
    red = meta.get("redactions") or {}
    red_total = sum(r.count for r in red.values()) if red else 0

    if red_total:
        items = ", ".join(f"{k} &times;{v.count}" for k, v in sorted(red.items()))
        banner = (f'<p class="banner"><b>{red_total} secret'
                  f'{"" if red_total == 1 else "s"} removed before this file was written.</b> '
                  f'{items}. Each one is shown in place as a marker, so you can see where a '
                  f'value was taken out. The check that produced this count is written '
                  f'independently of the code that does the removing, and it runs against this '
                  f'finished file rather than against the transcript.</p>')
    else:
        banner = ('<p class="banner safe"><b>No secrets were found to remove.</b> The output '
                  'was scanned after rendering and nothing credential-shaped survived, which '
                  'is the same check that would have blocked the export.</p>')

    total_calls = total.priced_calls + total.estimated_calls + total.unpriced_calls
    price_parts = [
        '<b>This is an API-equivalent list-price estimate in USD, not an invoice.</b> '
        f'Rates: {html.escape(RATE_SOURCE)}. Subscription plans, discounts, and other billing '
        'adjustments are not represented.'
    ]
    if total.estimated_calls:
        models = ", ".join(sorted(total.estimated_models))
        price_parts.append(
            f'{total.estimated_calls:,} of {total_calls:,} billed responses used family-rate '
            f'estimates for {html.escape(models)}.'
        )
    if total.unpriced_calls:
        models = ", ".join(sorted(total.unpriced_models)) or "an unidentified model"
        price_parts.append(
            f'<b>The displayed dollar total is incomplete.</b> {total.unpriced_calls:,} of '
            f'{total_calls:,} billed responses had no known rate and were omitted: '
            f'{html.escape(models)}. Their tokens remain in the token total.'
        )
    price_note = f'<p class="banner">{" ".join(price_parts)}</p>'
    cost_label = (
        "priced API estimate (USD)" if total.unpriced_calls
        else "API-equivalent estimate (USD)"
    )

    accounting_stats = (
        f'<div class="stat"><b>{total.total_tokens / 1e6:,.1f}M</b><span>tokens</span></div>'
        f'<div class="stat"><b>${total.dollars:,.2f}</b><span>{cost_label}</span></div>'
    )
    if not meta.get("accounting_available", True):
        accounting_stats = ''
        price_note = '<p class="banner">Token use and cost were not supplied. No price estimate is made.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(meta.get('title', 'Agent run replay'))}</title>
<meta name="robots" content="noindex">
<style>{CSS}</style>
</head>
<body>
<header><div class="wrap">
  <h1>{html.escape(meta.get('title', 'Agent run replay'))}</h1>
  <p class="sub">{html.escape(meta.get('subtitle', ''))}</p>
  <div class="stats">
    <div class="stat"><b>{len(payload):,}</b><span>events</span></div>
    <div class="stat"><b>{sum(1 for e in events if e.kind == 'tool_use'):,}</b><span>tool calls</span></div>
    <div class="stat"><b>{sum(1 for e in events if e.is_error):,}</b><span>errors</span></div>
    {accounting_stats}
    <div class="stat"><b>{red_total}</b><span>secrets removed</span></div>
  </div>
  {banner}
  {price_note}
</div></header>

<div class="controls"><div class="wrap">
  <div class="row">
    <input type="search" id="q" placeholder="Search this run" aria-label="Search the run">
    <select id="kind" aria-label="Filter by event kind"><option value="">All kinds</option>
      {''.join(f'<option value="{html.escape(k)}">{html.escape(k)}</option>' for k in kinds)}
    </select>
    <select id="tool" aria-label="Filter by tool"><option value="">All tools</option>
      {''.join(f'<option value="{html.escape(t)}">{html.escape(t)}</option>' for t in tools)}
    </select>
    <label style="font-size:.8rem;display:flex;gap:.3rem;align-items:center">
      <input type="checkbox" id="onlyerr"> errors only</label>
    <button type="button" id="expand">Expand all</button>
    <button type="button" id="collapse">Collapse all</button>
    <button type="button" id="theme">Theme: auto</button>
    <span class="pos" id="pos"></span>
  </div>
  <input type="range" id="scrub" min="0" max="0" value="0" aria-label="Scrub through the run">
</div></div>

<main><div class="wrap"><div id="list"></div></div></main>

<footer><div class="wrap">
  <p>Exported by <a href="https://github.com/JesseRWeigel/agent-replay">agent-replay</a>.
  This file is self-contained: no network requests, no external assets. Thinking blocks are
  {'included' if meta.get('thinking') else 'excluded by default'}.</p>
</div></footer>

<script id="events" type="application/json">{
  json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
    .replace('</', '<\\/')
}</script>
<script>{JS}</script>
</body>
</html>
"""
