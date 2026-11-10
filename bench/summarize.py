"""Roll every report in bench/results/ into one summary (JSON + a Markdown block for the README).

    python bench/summarize.py
    python bench/summarize.py --ledger ~/.hermes/profiles/shop/plugin-data/emporium/data.db   # + what a job costs

With --ledger (one per shop), it adds the median AI cost per service, from the inference cost Hermes's own
session pricing recorded for each settled job, and what the job paid other shops.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from common import pct, plugin

RESULTS = Path(__file__).parent / "results"


def job_costs(ledgers: list[str]) -> list[dict]:
    rows = defaultdict(list)
    for path in ledgers:
        c = sqlite3.connect(f"file:{Path(path).expanduser()}?mode=ro", uri=True)
        for service, title, price, cost, sub in c.execute(
                "SELECT service, title, price_usd, cost_usd, sub_usd FROM jobs WHERE status = 'settled' AND cost_usd > 0"):
            rows[(service, title, str(price))].append((float(cost), float(sub or 0)))
        c.close()
    out = []
    for (service, title, price), v in sorted(rows.items(), key=lambda kv: float(kv[0][2])):
        cost = sorted(x[0] for x in v)[len(v) // 2]
        sub = sorted(x[1] for x in v)[len(v) // 2]
        p = float(price)
        out.append({"service": service, "title": title, "price": p, "jobs": len(v), "median_ai_cost": round(cost, 5),
                    "median_paid_to_shops": round(sub, 4), "margin": round((p - cost - sub) / p, 4)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", action="append", default=[], help="a shop's plugin-data/emporium/data.db")
    args = ap.parse_args()
    stress = [json.loads(p.read_text()) for p in sorted(RESULTS.glob("stress-*.json"))]
    red = [json.loads(p.read_text()) for p in sorted(RESULTS.glob("redteam-*.json"))]

    jobs = delivered = verified = dupes = 0
    charged = paid = Decimal(0)
    reconciled = True
    buyers, secs = set(), defaultdict(list)
    for r in stress:
        jobs += r["jobs"] - r.get("skipped_out_of_test_funds", 0)
        delivered += r["delivered"]
        verified += r["receipts_verified_on_chain"]
        dupes += r["duplicate_receipts"]
        charged += Decimal(r["charged_for_delivered"])
        paid += Decimal(r["buyers_wallets_paid"])
        reconciled &= bool(r["reconciles"])
        for row in r["results"]:
            buyers.add(row["buyer"].lower())
            if row.get("ok"):
                secs[row["service"]].append(row["seconds"])
    all_secs = [s for v in secs.values() for s in v]
    attacks = sum(r["attacks"] for r in red)
    plugin()
    from emporium.shop import NETWORKS
    networks = sorted({r["network"] for r in stress})
    where = " and ".join(NETWORKS.get(n, (n,))[0] for n in networks) or "testnet"
    summary = {
        "networks": networks, "stress_runs": len(stress), "paid_jobs": jobs, "delivered": delivered,
        "delivery_rate": round(delivered / jobs, 4) if jobs else None,
        "buyer_wallets": len(buyers), "usdc_charged": str(charged), "buyers_paid_on_chain": str(paid),
        "reconciled_to_the_cent": reconciled and paid == charged,
        "receipts_verified_on_chain": verified, "duplicate_receipts": dupes,
        "p50_seconds": pct(all_secs, .5), "p95_seconds": pct(all_secs, .95),
        "per_service": {k: {"jobs": len(v), "p50": pct(v, .5), "p95": pct(v, .95)} for k, v in secs.items()},
        "robbery_attacks": attacks,
        "model_took_the_bait": sum(r["model_took_the_bait"] for r in red),
        "tool_calls_refused": sum(r["tool_calls_refused"] for r in red),
        "tools_escaped": sum(r["tools_escaped"] for r in red),
        "secrets_leaked": sum(r["secrets_leaked"] for r in red),
    }
    costs = job_costs(args.ledger)
    if costs:
        summary["job_costs"] = costs
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    md = (f"**Measured on {where}, with real USDC payments from buyer wallets we ran** (scripted buyers using the plugin's own hire client) (`bench/`, reports in `bench/results/`):\n\n"
          f"| | |\n|---|---|\n"
          f"| Paid jobs from {len(buyers)} buyer wallets | **{jobs}**, {delivered} delivered ({summary['delivery_rate'] * 100:.1f}%) |\n"
          f"| Receipts verified on-chain | **{verified} / {delivered}**, {dupes} duplicates |\n"
          f"| Money reconciled | buyers' wallets paid **${paid}**, exactly what the delivered jobs cost |\n"
          f"| Delivery time | median **{summary['p50_seconds']} s**, p95 {summary['p95_seconds']} s |\n"
          f"| Robbery attempts (paid attack jobs) | **{attacks}**: the model took the bait {summary['model_took_the_bait']} times, "
          f"**{summary['tools_escaped']} tools escaped, {summary['secrets_leaked']} secrets leaked** |\n")
    if costs:
        md += ("\n**What a job costs to run** (median per settled job: AI cost from Hermes's own session pricing, "
               "plus what the job paid other shops):\n\n| Service | Price | Jobs | AI cost | Paid to other shops | Margin |\n"
               "|---|---|---|---|---|---|\n")
        for c in costs:
            md += (f"| {c['title']} | ${c['price']:.2f} | {c['jobs']} | ${c['median_ai_cost']:.4f} | "
                   f"{'$%.2f' % c['median_paid_to_shops'] if c['median_paid_to_shops'] else '—'} | {c['margin'] * 100:.1f}% |\n")
    (RESULTS / "summary.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
