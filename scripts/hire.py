"""Hire a shop once from a NEAR testnet account, with the plugin's own buyer code: the shop answers 402,
your key signs a USDC transfer (a NEP-366 delegate: no gas needed), the job runs, and you get the result
with its on-chain receipt. A failed job costs nothing.

    export EMPORIUM_NEAR_BUYER_KEY=ed25519:…        # the account's full-access key; never on the command line
    python scripts/hire.py https://shop-production-8cc3cf57.up.railway.app code-review \
        "def is_even(n): return n % 2 == 1" --account you.testnet

The account needs a little testnet USDC (faucet.circle.com, "NEAR Testnet") and must be registered with
the USDC contract; `scripts/near_accounts.py setup` does both for accounts it creates.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shop")
    ap.add_argument("service")
    ap.add_argument("text")
    ap.add_argument("--account", required=True, help="your NEAR account (it pays)")
    ap.add_argument("--max", default="$1.00", help="the most this one job may cost")
    ap.add_argument("--network", default="near:testnet", choices=["near:testnet", "near:mainnet"],
                    help="the network this buyer may pay on (mainnet spends real USDC)")
    args = ap.parse_args()
    key = os.environ.get("EMPORIUM_NEAR_BUYER_KEY", "")
    if not key.startswith("ed25519:"):
        sys.exit("Put the account's key in EMPORIUM_NEAR_BUYER_KEY (ed25519:…).")
    spec = importlib.util.spec_from_file_location("emporium", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["emporium"] = mod
    spec.loader.exec_module(mod)
    from emporium import buyer

    limits = {"buyer_max_per_job": args.max, "buyer_daily_budget": "$5.00", "buyer_networks": [args.network],
              "buyer_allow_hosts": ["127.0.0.1", "localhost"]}
    policy = buyer.Policy(lambda k, d=None: limits.get(k, d))
    t = time.time()
    try:
        out = buyer.hire(args.shop, args.service, args.text, policy=policy, key=None, ledger=None,
                         near_account=args.account, near_key=key)
    except buyer.HireError as e:
        sys.exit(f"not delivered: {e}")
    print(out["output"])
    print("\n" + json.dumps({"seconds": round(time.time() - t, 1), "receipt": out["receipt"]}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
