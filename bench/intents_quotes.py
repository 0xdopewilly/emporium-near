"""What moving money across chains through NEAR Intents costs, measured with real 1Click quotes.

Asks 1Click for *dry* quotes only (``dry: true``: no deposit address is made and nothing can be paid),
for every reasonable route in both directions:

- fund:    USDC / USDT on each chain 1Click supports, and each chain's native coin (ETH, SOL, BTC, wNEAR,
           BNB, …) -> exactly $1 and $10 of USDC on NEAR (EXACT_OUTPUT, what /shop fund asks);
- cashout: exactly $1 and $10 of USDC on NEAR -> the same assets (EXACT_INPUT, what /shop cashout asks).

Every quote goes through the plugin's own checks (intents.check_quote: 1Click's signature, then the
quote is the one asked for), with the fee cap off so the fee itself is measured. Writes
bench/results/intents-quotes.json and bench/results/intents-quotes.md.

    python bench/intents_quotes.py                 # every route, 3 at a time
    python bench/intents_quotes.py --amounts 1 10 --workers 3 --chains base arb sol
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from common import plugin

RESULTS = Path(__file__).parent / "results"
NATIVE_SKIP = ("(DEPRECATED)", "(OMNI)")


def _b58check(payload: bytes) -> str:
    plugin()
    from emporium.near import b58encode
    return b58encode(payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4])


_H160 = hashlib.sha256(b"emporium bench: a dry-quote address nobody controls").digest()[:20]
# Well-formed addresses for dry quotes only (a dry quote makes no deposit address; nothing is ever sent
# to or refunded at these). Base58Check ones are built over a hash nobody holds a key for.
EXTRA_SAMPLES = {
    "doge": _b58check(b"\x1e" + _H160), "ltc": _b58check(b"\x30" + _H160), "dash": _b58check(b"\x4c" + _H160),
    "bch": _b58check(b"\x00" + _H160), "zec": _b58check(b"\x1c\xb8" + _H160),
    "starknet": "0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d",
    "movement": "0x000000000000000000000000000000000000000000000000000000000000000a",
    "hypercore": "0xa11ce57b2f0c88de41a9b3c07e6d1f45a3c0d9f2", "fogo": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
}


def sample(intents, chain: str) -> str:
    return intents.SAMPLE_ADDRESS.get(intents.chain_family(chain)) or EXTRA_SAMPLES.get(chain, "")


def assets(intents, tokens, chains=None) -> list:
    """Per chain: USDC, USDT (or USDT0) and the native coin; wNEAR stands for NEAR."""
    picked, seen = [], set()
    for chain in sorted({t.chain for t in tokens}):
        if chains and chain not in chains:
            continue
        on = [t for t in tokens if t.chain == chain]
        for spec in ("usdc", "usdt"):
            try:
                t = intents.resolve(tokens, f"{chain}:{spec}")
            except intents.IntentsError:
                continue
            if t.asset_id not in seen:
                picked.append(t)
                seen.add(t.asset_id)
        natives = [t for t in on if not t.contract and not any(s in t.symbol for s in NATIVE_SKIP)]
        if chain == "near":
            natives = [t for t in on if t.asset_id == "nep141:wrap.near"]
        for t in natives[:1]:
            if t.asset_id not in seen:
                picked.append(t)
                seen.add(t.asset_id)
    return [t for t in picked if t.asset_id != intents.NEAR_USDC]


def one(intents, client, direction: str, token, usd: str) -> dict:
    """One dry quote, checked the way the plugin checks a quote before paying it. ``ok`` means 1Click
    quoted it, its signature verified and it is the quote that was asked for."""
    row = {"direction": direction, "chain": token.chain, "symbol": token.symbol, "asset": token.asset_id,
           "decimals": token.decimals, "usd": usd, "ok": False, "quoted": False}
    addr = sample(intents, token.chain)
    if not addr:
        row["error"] = "no sample address for this chain in the bench"
        return row
    near_usdc = intents.resolve(client.tokens(), intents.NEAR_USDC)
    t0 = time.time()
    try:
        amount = intents.usd_atomic(usd)
        if direction == "fund":
            src, dst = token, near_usdc
            req = intents.quote_request(dry=True, swap_type="EXACT_OUTPUT", origin=src, destination=dst, amount=amount,
                                        refund_to=intents.check_address(token.chain, addr), recipient="intents.near",
                                        window=7200 if token.chain in ("btc", "doge", "ltc", "bch", "zec", "dash") else 3600,
                                        slippage_bps=intents.default_slippage(src, dst))
        else:
            src, dst = near_usdc, token
            req = intents.quote_request(dry=True, swap_type="EXACT_INPUT", origin=src, destination=dst, amount=amount,
                                        refund_to="intents.near", recipient=intents.check_address(token.chain, addr),
                                        window=1800, slippage_bps=intents.default_slippage(src, dst))
        req, resp = intents.ask_quote(client, req)
    except intents.OneClickError as e:
        row.update(error=str(e)[:200], http=e.status, seconds=round(time.time() - t0, 2))
        if e.min_amount:
            row["min_usd"] = str(Decimal(e.min_amount) / 10**6)
        elif e.min_usd:
            row["min_usd"] = str(e.min_usd)
        return row
    except intents.IntentsError as e:
        row.update(error=str(e)[:200], seconds=round(time.time() - t0, 2))
        return row
    row.update(quoted=True, seconds=round(time.time() - t0, 2), signature_ok=intents.verify_quote(resp))
    if row["signature_ok"]:
        try:
            q = intents.check_quote(resp, req, src, dst, max_fee_bps=None)
        except intents.QuoteRejected as e:
            row.update(error=str(e)[:200], rejected_field=e.field)
            return row
        row["ok"] = True
    else:
        q = intents.Quote(req, resp, src, dst)  # measured, but the plugin would refuse to pay it
        row["error"] = "1Click's signature doesn't verify (the plugin refuses to pay such a quote)"
    expected, worst = q.fee_bps()
    s = q.summary()
    row.update(amount_in=s["amount_in"], amount_out=s["amount_out"], min_amount_in=s["min_amount_in"],
               min_amount_out=s["min_amount_out"], amount_in_usd=s["amount_in_usd"], amount_out_usd=s["amount_out_usd"],
               fee_pct=None if expected is None else float(expected / 100),
               fee_pct_worst=None if worst is None else float(worst / 100), eta_s=q.time_estimate,
               refund_fee=q.q.get("refundFee"), withdraw_fee=q.q.get("withdrawFee"),
               within_default_cap=worst is None or worst <= intents.DEFAULT_MAX_FEE_BPS)
    return row


def median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def summarize(rows: list[dict], amounts: list[str]) -> dict:
    ok = [r for r in rows if r["ok"]]
    quoted = [r for r in rows if r.get("quoted")]
    out = {"routes": len(rows), "routes_ok": len(ok), "routes_quoted": len(quoted),
           "signatures_verified": sum(1 for r in quoted if r.get("signature_ok")),
           "signature_failures": sorted({f"{r['direction']} {r['chain']}:{r['symbol']}" for r in quoted if not r.get("signature_ok")}),
           "chains_tried": sorted({r["chain"] for r in rows}), "chains_ok": sorted({r["chain"] for r in ok})}
    for d in ("fund", "cashout"):
        sub = [r for r in ok if r["direction"] == d]
        stable = [r for r in sub if r["symbol"].upper().rstrip("0") in ("USDC", "USDT")]
        out[d] = {
            "routes_ok": len(sub), "routes": sum(1 for r in rows if r["direction"] == d),
            "median_fee_pct": median([r["fee_pct"] for r in sub]),
            "median_fee_pct_worst": median([r["fee_pct_worst"] for r in sub]),
            "median_eta_s": median([r["eta_s"] for r in sub]),
            "stablecoins": {"routes_ok": len(stable), "median_fee_pct": median([r["fee_pct"] for r in stable]),
                            "median_eta_s": median([r["eta_s"] for r in stable])},
            "per_amount": {a: {"routes_ok": sum(1 for r in sub if r["usd"] == a),
                               "median_fee_pct": median([r["fee_pct"] for r in sub if r["usd"] == a]),
                               "within_3pct_cap": sum(1 for r in sub if r["usd"] == a and r["within_default_cap"])}
                           for a in amounts},
        }
    mins = {}
    for r in rows:
        if r.get("min_usd"):
            key = f"{r['direction']} {r['chain']}:{r['symbol']}"
            mins[key] = max(mins.get(key, "0"), r["min_usd"], key=Decimal)
    out["minimums_usd"] = dict(sorted(mins.items(), key=lambda kv: Decimal(kv[1])))
    errors = {}
    for r in rows:
        if not r["ok"] and not r.get("min_usd") and r.get("signature_ok") is not False:
            errors.setdefault(r["error"][:90], []).append(f"{r['direction']} {r['chain']}:{r['symbol']} ${r['usd']}")
    out["other_errors"] = errors
    return out


def markdown(s: dict, rows: list[dict], amounts: list[str], when: str) -> str:
    f = lambda x: "—" if x is None else f"{x:.2f}%"  # noqa: E731
    lines = [f"**NEAR Intents (1Click), measured with real dry quotes** ({when}; `bench/intents_quotes.py`, "
             "every quote checked by the plugin's own `intents.check_quote`: 1Click's signature, then the terms)", "",
             "| | |", "|---|---|",
             f"| Routes quoted | **{s['routes_ok']} / {s['routes']}** ({', '.join('$' + a for a in amounts)}; both directions) |",
             f"| 1Click signatures verified | **{s['signatures_verified']} / {s['routes_quoted']}** quotes"
             + (f" (failing, so refused: {', '.join(s['signature_failures'])})" if s["signature_failures"] else "") + " |",
             f"| Chains with a working route | **{len(s['chains_ok'])}** of {len(s['chains_tried'])} tried: {', '.join(s['chains_ok'])} |"]
    for d, label in (("fund", "Fund (any chain → USDC on NEAR, EXACT_OUTPUT)"), ("cashout", "Cash out (USDC on NEAR → any chain, EXACT_INPUT)")):
        x = s[d]
        lines.append(f"| {label} | {x['routes_ok']}/{x['routes']} OK · median fee **{f(x['median_fee_pct'])}** "
                     f"(stablecoins {f(x['stablecoins']['median_fee_pct'])}) · median ETA **{x['median_eta_s']} s** |")
    lines += ["", "Fee = 1Click's USD value in vs out at quote time, before the slippage buffer (on fund, the unused "
              "buffer is refunded). ETA = 1Click's estimate once the deposit confirms. The bench quotes with stand-in "
              "addresses (a dry quote makes no deposit address); 1Click answers some of them on aptos, starknet, ton, "
              "tron and xrp with \"Internal server error\", as it does for a NEAR recipient that doesn't exist, so "
              "those chains are under-counted here.", "",
              "| Direction | Amount | Routes OK | Median fee | Inside the default 3% cap |", "|---|---|---|---|---|"]
    for d in ("fund", "cashout"):
        for a in amounts:
            p = s[d]["per_amount"][a]
            lines.append(f"| {d} | ${a} | {p['routes_ok']} | {f(p['median_fee_pct'])} | {p['within_3pct_cap']} |")
    if s["minimums_usd"]:
        lines += ["", "Route minimums 1Click reported (USD of USDC on NEAR): " +
                  ", ".join(f"{k} ${Decimal(v):.2f}" for k, v in list(s["minimums_usd"].items())[:14])]
    lines += ["", "| Route ($10) | In | Out | Fee | ETA |", "|---|---|---|---|---|"]
    for r in sorted((r for r in rows if r["ok"] and r["usd"] == amounts[-1]), key=lambda r: (r["direction"], r["fee_pct"] or 0)):
        src, dst = (f"{r['chain']}:{r['symbol']}", "near:USDC") if r["direction"] == "fund" else ("near:USDC", f"{r['chain']}:{r['symbol']}")
        lines.append(f"| {src} → {dst} | {r['amount_in']} | {r['amount_out']} | {f(r['fee_pct'])} | {r['eta_s']} s |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--amounts", nargs="+", default=["1", "10"])
    ap.add_argument("--workers", type=int, default=3, help="quotes in flight at once (be polite)")
    ap.add_argument("--chains", nargs="*", help="only these chains")
    ap.add_argument("--out", default=str(RESULTS / "intents-quotes.json"))
    args = ap.parse_args()
    plugin()
    from emporium import intents
    client = intents.OneClick(jwt=os.environ.get(intents.JWT_SECRET, ""), retries=4, backoff=2.0)
    tokens = client.tokens(fresh=True)
    picked = assets(intents, tokens, set(args.chains or []))
    jobs = [(d, t, a) for t in picked for d in ("fund", "cashout") for a in args.amounts]
    print(f"{len(picked)} assets on {len({t.chain for t in picked})} chains -> {len(jobs)} dry quotes, {args.workers} at a time")
    started = time.time()
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 6))) as pool:
        for r in pool.map(lambda j: one(intents, client, *j), jobs):
            rows.append(r)
            mark = (f"{r['fee_pct']:.2f}% {r['eta_s']}s" if r.get("fee_pct") is not None else "") if r["ok"] else (r.get("error") or "")[:70]
            print(f"  {'ok ' if r['ok'] else 'no '} {r['direction']:<7} {r['chain'] + ':' + r['symbol']:<22} ${r['usd']:<4} {mark}")
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    s = summarize(rows, args.amounts)
    report = {"measured": when, "seconds": round(time.time() - started, 1), "api": client.url,
              "jwt": bool(client.jwt), "amounts_usd": args.amounts, "summary": s, "routes": rows}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n")
    md = out.with_suffix(".md")
    md.write_text(markdown(s, rows, args.amounts, when))
    print(f"\n{s['routes_ok']}/{s['routes']} routes OK on {len(s['chains_ok'])} chains · fund median fee "
          f"{s['fund']['median_fee_pct']}% · cashout median fee {s['cashout']['median_fee_pct']}% · "
          f"median ETA {s['fund']['median_eta_s']}s / {s['cashout']['median_eta_s']}s\nwrote {out} and {md}")


if __name__ == "__main__":
    main()
