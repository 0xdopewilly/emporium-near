"""The shop window: one self-contained HTML page at GET / plus a JSON feed at GET /feed.

No inputs, outputs or full wallet addresses are ever shown here; the feed carries service, price,
status, a shortened payer and the settlement tx.
"""

from __future__ import annotations

import html
import json
import time
from decimal import Decimal
from typing import Any

from .shop import Shop, usd

_STATUS_LABEL = {"settled": "Delivered · paid", "working": "Working", "paid": "Paid", "failed": "Not charged",
                 "unpaid": "Unsettled", "refused": "Refused"}


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else (addr or "—")


def feed(shop: Shop, books: Any, limit: int = 12) -> dict[str, Any]:
    now = time.time()
    s = books.sales_summary()
    jobs = []
    for j in books.recent_jobs(limit):
        jobs.append({
            "id": j["id"], "service": j["service"], "title": j["title"], "price": usd(Decimal(j["price_usd"])),
            "status": j["status"], "label": _STATUS_LABEL.get(j["status"], j["status"]),
            "payer": _short(j["payer"]), "tx": j["tx"], "explorer": shop.explorer(j["tx"]),
            "age": max(0, int(now - (j["finished"] or j["created"]))),
            "seconds": round((j["finished"] - j["started"]), 1) if j["finished"] and j["started"] else None,
        })
    return {"open": shop.open and books.is_open(), "earned_total": usd(s["earned_total"]) if s["earned_total"] else "$0.00",
            "earned_today": usd(s["earned_today"]) if s["earned_today"] else "$0.00", "jobs_settled": s["jobs_settled"],
            "buyers": s["buyers"], "jobs": jobs}


def render(shop: Shop, books: Any) -> str:
    e = html.escape
    base = shop.base_url()
    cards = []
    for s in shop.services:
        tools = "".join(f'<span class="chip">{e(t.replace("_", " "))}</span>' for t in s.tools) or \
            '<span class="chip quiet">no tools · pure skill</span>'
        cards.append(f"""
      <article class="svc">
        <header><h3>{e(s.title)}</h3><div class="price">{e(s.price_label)}<small>USDC / job</small></div></header>
        <p>{e(s.description)}</p>
        <div class="chips">{tools}</div>
        <code class="ep">POST {e(base)}/jobs/{e(s.id)}</code>
      </article>""")
    data = json.dumps({"feed": f"{base}/feed"})
    testnet = '<span class="pill test">testnet</span>' if shop.testnet else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(shop.name)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0d0c0a;--card:#16140f;--line:#2a261d;--ink:#f3ecdf;--mute:#a59c8a;--gold:#e9b949;--ok:#45d99a;--bad:#ff7a66;--work:#9f97ff}}
*{{box-sizing:border-box}}html,body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 Inter,system-ui,sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:44px 28px 64px}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;flex-wrap:wrap}}
.brand{{display:flex;gap:16px;align-items:center}}
.mark{{width:52px;height:52px;border-radius:14px;background:radial-gradient(circle at 30% 30%,#ffd977,#c98a1c);display:grid;place-items:center;color:#1a1408;font:italic 30px 'Instrument Serif',serif;box-shadow:0 0 0 1px #0006 inset,0 10px 30px #e9b94933}}
h1{{font:400 46px/1.05 'Instrument Serif',Georgia,serif;margin:0;letter-spacing:-.5px}}
.tag{{color:var(--mute);margin:10px 0 0;max-width:640px}}
.pills{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}}
.pill{{border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:13px;color:var(--mute);background:#ffffff05}}
.pill.open{{color:var(--ok);border-color:#45d99a55}}.pill.closed{{color:var(--bad);border-color:#ff7a6655}}.pill.test{{color:var(--gold);border-color:#e9b94955}}
.stats{{display:grid;grid-template-columns:repeat(3,minmax(130px,1fr));gap:12px;min-width:min(100%,430px)}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 16px}}
.stat b{{display:block;font:500 26px 'JetBrains Mono',monospace;letter-spacing:-.5px}}.stat span{{color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.08em}}
.stat.gold b{{color:var(--gold)}}
h2{{font:400 28px 'Instrument Serif',Georgia,serif;margin:44px 0 14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}}
.svc{{background:linear-gradient(180deg,#1b1812,#141210);border:1px solid var(--line);border-radius:20px;padding:20px 20px 18px;display:flex;flex-direction:column;gap:10px}}
.svc header{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}
.svc h3{{margin:0;font:600 17px Inter,sans-serif}}.svc p{{margin:0;color:var(--mute)}}
.price{{font:500 24px 'JetBrains Mono',monospace;color:var(--gold);text-align:right;white-space:nowrap}}.price small{{display:block;font:11px Inter;color:var(--mute);letter-spacing:.06em}}
.chips{{display:flex;gap:6px;flex-wrap:wrap}}.chip{{font-size:12px;border-radius:8px;padding:2px 8px;background:#9f97ff1a;color:#c9c4ff}}.chip.quiet{{background:#ffffff0a;color:var(--mute)}}
.ep{{font:12px 'JetBrains Mono',monospace;color:#cfc6b4;background:#0b0a08;border:1px solid var(--line);border-radius:10px;padding:8px 10px;overflow-wrap:anywhere;margin-top:auto}}
.how{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}}
.step{{border:1px dashed var(--line);border-radius:16px;padding:14px 16px;color:var(--mute)}}.step b{{color:var(--ink);display:block;margin-bottom:4px}}
.feed{{border:1px solid var(--line);border-radius:20px;overflow:hidden}}
.row{{display:grid;grid-template-columns:1.6fr .7fr 1fr 1.2fr .6fr;gap:12px;padding:12px 18px;border-top:1px solid var(--line);align-items:center;font-size:14px}}
.row:first-child{{border-top:0}}.row.head{{color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.08em;background:#ffffff04}}
.row .m{{font-family:'JetBrains Mono',monospace;font-size:13px}}.row a{{color:var(--gold);text-decoration:none}}
.st{{font-size:12px;border-radius:999px;padding:2px 10px;display:inline-block}}.st.settled{{background:#45d99a1f;color:var(--ok)}}.st.working{{background:#9f97ff22;color:#c9c4ff}}.st.failed,.st.unpaid,.st.refused{{background:#ff7a661c;color:var(--bad)}}
.row.new{{animation:pop 1.2s ease}}@keyframes pop{{0%{{background:#e9b94930}}100%{{background:transparent}}}}
.empty{{padding:22px 18px;color:var(--mute)}}
footer{{margin-top:40px;color:var(--mute);font-size:13px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}}
@media (max-width:640px){{h1{{font-size:36px}}.row{{grid-template-columns:1.4fr .8fr 1fr}}.row .hide{{display:none}}.stats{{grid-template-columns:repeat(3,1fr)}}}}
</style></head><body><div class="wrap">
<div class="top">
  <div>
    <div class="brand"><div class="mark">H</div><h1>{e(shop.name)}</h1></div>
    <p class="tag">{e(shop.tagline)}</p>
    <div class="pills"><span class="pill" id="openpill">…</span><span class="pill">USDC on {e(shop.network_label)}</span>{testnet}<span class="pill">x402</span></div>
  </div>
  <div class="stats">
    <div class="stat gold"><b id="earned">$0.00</b><span>earned</span></div>
    <div class="stat"><b id="jobs">0</b><span>jobs delivered</span></div>
    <div class="stat"><b id="buyers">0</b><span id="buyerslbl">buyers</span></div>
  </div>
</div>
<h2>On the menu</h2>
<div class="grid">{''.join(cards)}</div>
<h2>How hiring works</h2>
<div class="how">
  <div class="step"><b>1 · Ask</b>POST your input to a service. The shop answers 402 with the price.</div>
  <div class="step"><b>2 · Sign</b>Your wallet signs a USDC authorization. Nothing moves yet.</div>
  <div class="step"><b>3 · Work</b>This Hermes does the job in a sealed session: no access to its owner's files, memory or keys.</div>
  <div class="step"><b>4 · Pay on delivery</b>The result comes back with the on-chain receipt. Failed jobs are never charged.</div>
</div>
<h2>Recently served</h2>
<div class="feed" id="feed"><div class="empty">No jobs yet. Be the first.</div></div>
<footer><span>Wallet <span class="m">{e(shop.pay_to)}</span></span><span>Powered by Hermes Emporium · <a style="color:var(--gold)" href="{e(base)}/.well-known/emporium.json">catalog.json</a></span></footer>
</div>
<script>
const CFG={data};let seen=new Set(),first=true;
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}})[c]);
function ago(s){{return s<60?s+"s ago":s<3600?Math.floor(s/60)+"m ago":Math.floor(s/3600)+"h ago"}}
async function tick(){{try{{const r=await fetch(CFG.feed,{{cache:"no-store"}});const d=await r.json();
 const p=document.getElementById("openpill");p.textContent=d.open?"● Open for business":"● Closed";p.className="pill "+(d.open?"open":"closed");
 document.getElementById("earned").textContent=d.earned_total;document.getElementById("jobs").textContent=d.jobs_settled;document.getElementById("buyers").textContent=d.buyers;document.getElementById("buyerslbl").textContent=d.buyers===1?"buyer":"buyers";
 const f=document.getElementById("feed");if(!d.jobs.length)return;
 let h='<div class="row head"><span>Job</span><span>Price</span><span>Status</span><span class="hide">Receipt</span><span class="hide">When</span></div>';
 for(const j of d.jobs){{const fresh=!first&&!seen.has(j.id+j.status);seen.add(j.id+j.status);
  const rc=j.explorer?`<a href="${{esc(j.explorer)}}" target="_blank" rel="noopener">${{esc(j.tx.slice(0,10))}}…</a>`:'<span style="color:var(--mute)">—</span>';
  h+=`<div class="row${{fresh?" new":""}}"><span>${{esc(j.title)}} <span class="m" style="color:var(--mute)">· ${{esc(j.payer)}}</span></span><span class="m">${{esc(j.price)}}</span><span><span class="st ${{esc(j.status)}}">${{esc(j.label)}}</span></span><span class="m hide">${{rc}}</span><span class="hide" style="color:var(--mute)">${{ago(j.age)}}</span></div>`}}
 f.innerHTML=h;first=false}}catch(e){{}}}}
tick();setInterval(tick,2500);
</script></body></html>"""
