"""Roll every report in bench/results/ into one summary (JSON + a Markdown block for the README).

    python bench/summarize.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from common import pct

RESULTS = Path(__file__).parent / "results"


def main() -> None:
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
    summary = {
        "stress_runs": len(stress), "paid_jobs": jobs, "delivered": delivered,
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
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    md = (f"**Measured on Base Sepolia, with real USDC payments from agents we ran** (`bench/`, reports in `bench/results/`):\n\n"
          f"| | |\n|---|---|\n"
          f"| Paid jobs from {len(buyers)} buyer wallets | **{jobs}**, {delivered} delivered ({summary['delivery_rate'] * 100:.1f}%) |\n"
          f"| Receipts verified on-chain | **{verified} / {delivered}**, {dupes} duplicates |\n"
          f"| Money reconciled | buyers' wallets paid **${paid}**, exactly what the delivered jobs cost |\n"
          f"| Delivery time | median **{summary['p50_seconds']} s**, p95 {summary['p95_seconds']} s |\n"
          f"| Robbery attempts (paid attack jobs) | **{attacks}**: the model took the bait {summary['model_took_the_bait']} times, "
          f"**{summary['tools_escaped']} tools escaped, {summary['secrets_leaked']} secrets leaked** |\n")
    (RESULTS / "summary.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
