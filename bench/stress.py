"""Stress bench: many buyer wallets hire one or more shops at once, for real, on testnet. Then every
cent is reconciled on-chain.

    python bench/stress.py --wallets fleet.json --concurrency 8 \
        --plan http://127.0.0.1:9402/code-review=90 --plan http://127.0.0.1:9403/translate-yoruba=60

What it reports (bench/results/stress-<time>.json):
- delivered / not delivered, and why; busy refusals (never charged) are retried after a pause;
- latency per service (p50 / p95 / max);
- every receipt checked on-chain (a real USDC transfer of the price to the shop's wallet);
- reconciliation: what the buyers' wallets lost and the shops' wallets gained, against what the
  delivered jobs cost. Equal to the cent means no double charges and no charges for failed jobs.
"""

from __future__ import annotations

import argparse
import itertools
import json
import queue
import random
import threading
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from common import address, load_keys, pct, plugin, usdc_balance
from inputs import INPUTS

BUSY = ("all hands busy", "too many jobs", "HTTP 503", "HTTP 429")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wallets", required=True)
    ap.add_argument("--plan", action="append", required=True, help="<shop_url>/<service>=<jobs>")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    plugin()
    from emporium import buyer, chain

    keys = load_keys(args.wallets)
    wallets = [(k, address(k)) for k in keys]
    rnd = random.Random(args.seed)
    jobs = []
    for spec in args.plan:
        target, n = spec.rsplit("=", 1)
        url, service = target.rsplit("/", 1)
        texts = INPUTS[service]
        jobs += [(url, service, texts[i % len(texts)]) for i in range(int(n))]
    rnd.shuffle(jobs)
    shops = {}
    for url in {u for u, _, _ in jobs}:
        cat = buyer.catalog(url, fresh=True)
        shops[url] = {"name": cat["name"], "pay_to": cat["pay_to"], "network": cat["network"],
                      "prices": {s["id"]: Decimal(s["price_usd"]) for s in cat["services"]}}
    network = next(iter(shops.values()))["network"]

    print(f"{len(jobs)} jobs · {len(wallets)} buyer wallets · {len(shops)} shop(s) · concurrency {args.concurrency}")
    print("reading balances…", flush=True)
    before_buyers = {a: usdc_balance(network, a) for _, a in wallets}
    before_shops = {s["pay_to"]: usdc_balance(network, s["pay_to"]) for s in shops.values()}

    policy = buyer.Policy(lambda k, d=None: {"buyer_max_per_job": "$5", "buyer_daily_budget": "$100000"}.get(k, d))
    todo: queue.Queue = queue.Queue()
    for i, j in enumerate(jobs):
        todo.put((i, *j, 0))
    results, lock = [], threading.Lock()
    wallet_cycle = itertools.cycle(range(len(wallets)))
    busy = [False] * len(wallets)
    left = [before_buyers[a] for _, a in wallets]   # what each wallet can still spend, reserved per job
    skipped = []
    started = time.time()

    def take_wallet(price: Decimal):
        """A free wallet that can afford the job (reserving the price), or None if no wallet can."""
        for _ in range(len(wallets)):
            w = next(wallet_cycle)
            if not busy[w] and left[w] >= price:
                busy[w] = True
                left[w] -= price
                return w
        return None if not any(l >= price for l in left) else -1

    def worker() -> None:
        while True:
            try:
                i, url, service, text, tries = todo.get_nowait()
            except queue.Empty:
                return
            price = shops[url]["prices"][service]
            with lock:
                w = take_wallet(price)
            if w is None:  # the fleet ran out of test funds: not a shop failure, reported apart
                with lock:
                    skipped.append(i)
                continue
            if w == -1:    # every wallet that could pay is mid-job: wait for one
                time.sleep(0.5)
                todo.put((i, url, service, text, tries))
                continue
            key, addr = wallets[w]
            t0 = time.time()
            rec = {"i": i, "shop": shops[url]["name"], "url": url, "service": service, "buyer": addr}
            try:
                r = buyer.hire(url, service, text, policy=policy, key=key, ledger=None)
                rec.update(ok=True, seconds=round(time.time() - t0, 2), tx=r["receipt"].get("tx", ""),
                           charged=r["charged_usd"], job=r.get("job", ""), output_chars=len(r["output"]))
                with lock:
                    busy[w] = False
            except buyer.HireError as e:
                msg = str(e)
                with lock:
                    busy[w] = False
                    left[w] += price  # not charged
                if any(b in msg for b in BUSY) and tries < 8:
                    time.sleep(2 + tries)
                    todo.put((i, url, service, text, tries + 1))
                    continue
                rec.update(ok=False, seconds=round(time.time() - t0, 2), error=msg[:240])
            with lock:
                results.append(rec)
                done = len(results)
                if done % 10 == 0 or done + len(skipped) == len(jobs):
                    ok = sum(1 for r in results if r.get("ok"))
                    print(f"  {done}/{len(jobs)} · {ok} delivered · {time.time() - started:.0f}s", flush=True)

    threads = [threading.Thread(target=worker) for _ in range(args.concurrency)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    wall = time.time() - started

    print("checking every receipt on-chain…", flush=True)
    delivered = [r for r in results if r.get("ok")]
    verified, bad = 0, []
    for r in delivered:
        try:
            got = chain.paid_to(network, r["tx"], shops[r["url"]]["pay_to"])
        except Exception:
            got = Decimal(0)
        if r["tx"] and got >= Decimal(r["charged"]):
            verified += 1
        else:
            bad.append(r["tx"])
    txs = [r["tx"] for r in delivered]
    time.sleep(4)  # let the last settlements reach the public RPC
    after_buyers = {a: usdc_balance(network, a) for _, a in wallets}
    after_shops = {s["pay_to"]: usdc_balance(network, s["pay_to"]) for s in shops.values()}

    paid_by_buyers = sum(before_buyers[a] - after_buyers[a] for a in before_buyers)
    charged = sum(Decimal(r["charged"]) for r in delivered)
    per_service = defaultdict(list)
    for r in delivered:
        per_service[f'{r["shop"]} · {r["service"]}'].append(r["seconds"])
    failures = defaultdict(int)
    for r in results:
        if not r.get("ok"):
            failures[r["error"].split(":")[0][:80]] += 1

    report = {
        "when": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "network": network, "wall_seconds": round(wall, 1),
        "jobs": len(jobs), "buyer_wallets": len(wallets), "concurrency": args.concurrency,
        "delivered": len(delivered), "not_delivered": len(results) - len(delivered),
        "delivery_rate": round(len(delivered) / max(1, len(results)), 4),
        "failures": dict(failures),
        "skipped_out_of_test_funds": len(skipped),
        "receipts_verified_on_chain": verified, "receipts_not_verified": bad,
        "duplicate_receipts": len(txs) - len(set(txs)),
        "charged_for_delivered": str(charged),
        "buyers_wallets_paid": str(paid_by_buyers),
        "reconciles": paid_by_buyers == charged,
        "shops": {s["name"]: {"pay_to": s["pay_to"], "wallet_gain": str(after_shops[s["pay_to"]] - before_shops[s["pay_to"]]),
                              "earned_in_run": str(sum(Decimal(r["charged"]) for r in delivered if r["url"] == u))}
                  for u, s in shops.items()},
        "latency": {k: {"jobs": len(v), "p50": pct(v, .5), "p95": pct(v, .95), "max": max(v)} for k, v in per_service.items()},
        "results": sorted(results, key=lambda r: r["i"]),
    }
    out = Path(args.out or Path(__file__).parent / "results" / f"stress-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=1))
    print("report:", out)


if __name__ == "__main__":
    main()
