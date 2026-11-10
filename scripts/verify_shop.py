"""Check a shop's sales against NEAR, with no keys and no account: read its catalog and job feed, then look
up each settled job's transaction on-chain and confirm a USDC transfer of the job's price to the shop.

    python scripts/verify_shop.py https://shop-production-8cc3cf57.up.railway.app
    python scripts/verify_shop.py http://127.0.0.1:9502 --jobs 20

Run it with a Python that has the plugin's dependencies (Hermes's venv works). A shop can claim anything on
its page; this prints only what the chain confirms.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def plugin():
    spec = importlib.util.spec_from_file_location("emporium", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["emporium"] = mod
    spec.loader.exec_module(mod)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shop", help="the shop's base URL")
    ap.add_argument("--jobs", type=int, default=12, help="how many recent settled jobs to check")
    args = ap.parse_args()
    plugin()
    from emporium.chain import near_paid
    from emporium.shop import NETWORKS, USDC, is_near

    url = args.shop.rstrip("/")
    cat = httpx.get(url + "/.well-known/emporium.json", timeout=20).json()
    feed = httpx.get(url + "/feed", timeout=20).json()
    network, pay_to, relayer = cat["network"], cat["pay_to"], cat.get("relayer", "")
    if not is_near(network):
        sys.exit(f"{cat['name']} is on {network}; this script checks NEAR shops")
    print(f"{cat['name']} · paid to {pay_to} on {NETWORKS[network][0]} · USDC {USDC[network][:8]}… · relayer {relayer}")
    print(f"The shop says: {feed.get('jobs_settled')} jobs delivered, {feed.get('earned_total')} earned.\n")
    jobs = [j for j in feed.get("jobs") or [] if j.get("status") == "settled" and j.get("tx")][: args.jobs]
    ok = 0
    total = Decimal(0)
    for j in jobs:
        price = Decimal(str(j["price"]).lstrip("$"))
        got = near_paid(network, j["tx"], relayer, "", pay_to)
        good = got >= price
        ok += good
        total += got if good else 0
        print(f"  {'✓' if good else '✗'} {j['title'][:34]:<34} {j['price']:>6}  on-chain {f'${got:.2f}':>8}  {j['explorer']}")
    print(f"\n{ok} of {len(jobs)} receipts confirmed on NEAR: ${total:.2f} in USDC reached {pay_to}.")
    sys.exit(0 if ok == len(jobs) else 1)


if __name__ == "__main__":
    main()
