"""Check the NEAR mainnet run in bench/results/mainnet-run.json from public data, with no keys:

    python scripts/verify_mainnet.py [bench/results/mainnet-run.json]

- every payment (68 paid jobs and 12 shop-to-shop payments): its NEAR transaction moved exactly that much
  USDC from the buyer to the shop, submitted by the shop's relayer (so the buyer paid no gas);
- the top-up: 1Click reports SUCCESS for its deposit address, and the Base transaction sent USDC to it;
- each cash-out: the NEAR transaction moved the USDC to 1Click's deposit address, 1Click reports SUCCESS,
  and the Base transaction delivered USDC to the recipient.

NEAR transactions come from NEAR's public RPC (an archival node once public nodes have pruned them),
Base receipts from public Base RPC, and the deposit statuses from 1Click's public status API.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BASE_RPCS = ("https://base-rpc.publicnode.com", "https://1rpc.io/base", "https://mainnet.base.org")
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ONE_CLICK_STATUS = "https://1click.chaindefuser.com/v0/status"


def plugin():
    spec = importlib.util.spec_from_file_location("emporium", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["emporium"] = mod
    spec.loader.exec_module(mod)


def base_usdc_to(tx: str, to: str) -> Decimal:
    """USDC the Base transaction ``tx`` transferred to ``to`` (0 if it failed or sent none)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx]}
    for url in BASE_RPCS:
        try:
            r = httpx.post(url, json=body, timeout=20, headers={"User-Agent": "emporium-verify"}).json().get("result")
        except (httpx.HTTPError, ValueError):
            continue
        if not r:
            continue
        if int(r["status"], 16) != 1:
            return Decimal(0)
        total = sum(int(log["data"], 16) for log in r["logs"]
                    if log["address"].lower() == BASE_USDC and log["topics"][0] == TRANSFER
                    and log["topics"][2][-40:].lower() == to.lower().removeprefix("0x"))
        return Decimal(total) / Decimal(10**6)
    raise SystemExit(f"no Base RPC answered for {tx}")


def oneclick(deposit: str) -> dict:
    return httpx.get(ONE_CLICK_STATUS, params={"depositAddress": deposit}, timeout=30).json()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("record", nargs="?", default=str(ROOT / "bench/results/mainnet-run.json"))
    run = json.loads(Path(ap.parse_args().record).read_text())
    plugin()
    from emporium.chain import near_paid

    net, bad = run["network"], []
    ok = {"job": 0, "subcontract": 0, "smoke": 0}
    total = Decimal(0)
    for i, p in enumerate(run["payments"], 1):
        got = near_paid(net, p["tx"], p["relayer"], p["payer"], p["pay_to"])
        if got == Decimal(p["usdc"]):
            ok[p["kind"]] += 1
            total += got
        else:
            bad.append(f"payment {p['tx']}: expected {p['usdc']} USDC, the chain shows {got}")
        if sys.stdout.isatty():
            print(f"\r  payments checked: {i}/{len(run['payments'])}", end="", flush=True)
    jobs = ok["job"] + ok["smoke"]
    n_jobs = sum(1 for p in run["payments"] if p["kind"] in ("job", "smoke"))
    n_sub = sum(1 for p in run["payments"] if p["kind"] == "subcontract")
    lead = "\r" if sys.stdout.isatty() else ""
    print(f"{lead}{jobs}/{n_jobs} paid jobs and {ok['subcontract']}/{n_sub} shop-to-shop payments confirmed on NEAR mainnet "
          f"(${total} of USDC, each submitted by the shop's relayer, so the payer spent no NEAR)")

    f = run["fund"]
    st = oneclick(f["deposit_address"])
    sent = sum(base_usdc_to(tx, f["deposit_address"]) for tx in f["base_txs"])
    fund_ok = st.get("status") == "SUCCESS" and sent >= Decimal(f["received_usdc_on_near"])
    print(f"top-up from Base: {sent} USDC sent on Base, 1Click {st.get('status')}, "
          f"{f['received_usdc_on_near']} USDC to {f['near_account'][:8]}… on NEAR  {'✓' if fund_ok else '✗'}")
    if not fund_ok:
        bad.append("the top-up did not check out")

    back = Decimal(0)
    for c in run["cashouts"]:
        moved = near_paid(net, c["near_tx"], c["relayer"], c["from"], c["deposit_address"])
        st = oneclick(c["deposit_address"])
        got = base_usdc_to(c["base_tx"], run["base_recipient"])
        good = moved == Decimal(c["usdc_in"]) and st.get("status") == "SUCCESS" and got == Decimal(c["usdc_out_on_base"])
        back += got
        print(f"cash-out ({c['shop']}): {moved} USDC left NEAR, 1Click {st.get('status')}, {got} USDC arrived on Base  {'✓' if good else '✗'}")
        if not good:
            bad.append(f"cash-out {c['near_tx']} did not check out")
    print(f"round trip: {sent} USDC in from Base, {back} USDC back to Base")

    if bad:
        print("\n".join(["", "NOT CONFIRMED:", *bad]))
        raise SystemExit(1)
    print("All confirmed on NEAR mainnet and Base.")


if __name__ == "__main__":
    main()
