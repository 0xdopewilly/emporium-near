"""Move USDC between NEAR and any other chain through NEAR Intents (1Click), with the plugin's own code
and outside Hermes. Mainnet only: NEAR Intents has no testnet.

    python scripts/intents.py tokens [--chain base]                     # what 1Click supports
    python scripts/intents.py quote 5 from base:usdc                    # dry: what topping up costs
    python scripts/intents.py quote 5 to sol:usdc <solana address>      # dry: what cashing out costs

    # A buyer tops up its own NEAR account with exactly $5 of USDC, from USDC on Base. Prints the deposit
    # address to pay (or, with --send, pays it from EMPORIUM_BUYER_KEY's Base wallet: USDC + a little ETH).
    python scripts/intents.py fund 5 from base:usdc --account buyer.near --refund 0xYourBaseAddress [--send]

    # A shop cashes out $5 of USDC on NEAR to USDC on Base. The shop's key signs the transfer, its relayer
    # registers 1Click's deposit address (0.00125 NEAR) and pays the gas. Shows the quote and asks first.
    export EMPORIUM_NEAR_PAYOUT_KEY=ed25519:…    # pay_to's full-access key
    export EMPORIUM_NEAR_RELAYER_KEY=ed25519:…   # the relayer's key
    python scripts/intents.py cashout 5 to base:usdc 0xYourBaseAddress --shop shop.near --relayer shop-relay.near

    python scripts/intents.py resume <deposit> --shop shop.near --relayer shop-relay.near   # finish an interrupted cash-out
    python scripts/intents.py status <deposit> [--wait 600]
    python scripts/intents.py transfers

Every quote is checked before anything is paid: 1Click's signature, and that it is exactly the quote
asked for. Every live quote is kept (with its signature) in --books (default .dev/intents.sqlite), and a
cash-out run twice, or resumed after a crash, never pays twice. EMPORIUM_ONECLICK_JWT is optional.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def plugin():
    if "emporium" not in sys.modules:
        spec = importlib.util.spec_from_file_location("emporium", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["emporium"] = mod
        spec.loader.exec_module(mod)
    from emporium import intents, ledger
    return intents, ledger


def key(name: str):
    from emporium.near import KeyPair
    text = os.environ.get(name, "")
    try:
        return KeyPair(text)
    except ValueError:
        sys.exit(f"Put the key in {name} (ed25519:…); keys never go on the command line.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["tokens", "quote", "fund", "cashout", "resume", "status", "transfers"])
    ap.add_argument("words", nargs="*", help="e.g. 5 from base:usdc, or 5 to base:usdc 0x…, or a deposit address")
    ap.add_argument("--account", help="fund: the NEAR account to top up (the buyer's own)")
    ap.add_argument("--refund", help="fund: your address on the origin chain (refunds go there)")
    ap.add_argument("--send", action="store_true", help="fund: pay the deposit from EMPORIUM_BUYER_KEY's EVM wallet")
    ap.add_argument("--shop", help="cashout: the shop's NEAR account (pay_to)")
    ap.add_argument("--relayer", help="cashout: the relayer account that registers the deposit address and pays gas")
    ap.add_argument("--yes", action="store_true", help="cashout: don't ask before sending")
    ap.add_argument("--wait", type=float, default=600.0, help="seconds to follow the transfer (0: don't)")
    ap.add_argument("--chain", help="tokens: only this chain")
    ap.add_argument("--rpc", default="", help="NEAR mainnet RPC (default: the plugin's public endpoints)")
    ap.add_argument("--books", default=str(ROOT / ".dev" / "intents.sqlite"), help="where quotes and transfers are kept")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_intermixed_args()
    intents, ledger = plugin()
    client = intents.OneClick(jwt=os.environ.get(intents.JWT_SECRET, ""))
    Path(args.books).parent.mkdir(parents=True, exist_ok=True)
    books = intents.Books(ledger.Ledger(args.books))
    say = (lambda *_a, **_k: None) if args.json else print

    def follow(deposit: str) -> dict:
        if args.wait <= 0:
            return intents.track(client, deposit, books=books)
        say(f"Following {deposit} at 1Click (up to {int(args.wait)}s)…")
        return intents.track(client, deposit, books=books, wait=args.wait, poll=5,
                             on_status=lambda s: say(f"  {s}"))

    try:
        if args.command == "tokens":
            rows = [t for t in client.tokens() if not args.chain or t.chain == args.chain]
            for t in sorted(rows, key=lambda t: (t.chain, t.symbol)):
                say(f"{t.chain}:{t.symbol.lower():<10} {t.asset_id}  ({t.decimals} decimals, ${t.price})")
            return
        if args.command == "transfers":
            for r in books.ledger.recent_transfers(20):
                say(f"{r['kind']:<7} ${r['usd']:>8}  {r['status']:<16} {r['deposit']}")
            return
        if args.command == "status":
            out = follow(args.words[0] if args.words else sys.exit("status <deposit address>"))
            print(json.dumps(out, indent=1))
            return
        if args.command == "resume":
            if not args.words:
                sys.exit("resume <deposit address>")
            row = books.ledger.find_transfer(args.words[0]) or sys.exit("no such transfer in the books")
            q = intents.quote_from_record(row, client)
            return cashout(intents, client, books, q, args, say, follow)

        p = intents.parse_words(args.words)
        limits = {k: v for k, v in (("slippage_bps", p["slippage"]), ("max_fee_bps", p["max_fee"])) if v is not None}
        if args.command == "quote":
            if p["direction"] == "from":
                q = intents.fund_quote(client, usd=p["usd"], origin=p["token"], dry=True, refund_to=p["refund"] or args.refund or "",
                                       recipient=p["recipient"] or args.account or intents.SAMPLE_ADDRESS["near"], **limits)
            else:
                dst = client.token(p["token"])
                q = intents.cashout_quote(client, usd=p["usd"], destination=p["token"], dry=True,
                                          recipient=p["recipient"] or intents.SAMPLE_ADDRESS.get(intents.chain_family(dst.chain), ""),
                                          shop=args.shop or intents.SAMPLE_ADDRESS["near"], **limits)
            if args.json:
                print(json.dumps(q.summary(), indent=1))
            else:
                print("NEAR Intents quote (dry: nothing to pay, no deposit address made):")
                print("\n".join(intents.quote_lines(q)))
            return
        if args.command == "fund":
            account = p["recipient"] or args.account or sys.exit("--account: the NEAR account to top up")
            buyer_key = os.environ.get("EMPORIUM_BUYER_KEY", "") if (args.send or p["send"]) else ""
            refund = p["refund"] or args.refund or (intents.evm_address(buyer_key) if buyer_key else "")
            q = intents.fund_quote(client, usd=p["usd"], origin=p["token"], recipient=account, refund_to=refund,
                                   rpc_url=args.rpc, **limits)
            intents.record(books, q)
            say(f"Top up {account} with exactly {intents.fmt_amount(q.amount_out, 6)} USDC on NEAR:")
            say("\n".join(intents.quote_lines(q)))
            o = q.origin
            if buyer_key:
                res = intents.evm_pay(client, q, buyer_key, books=books)
                say(f"Paid the deposit from {refund}: tx {res['tx']} ({'confirmed' if res['confirmed'] else 'not confirmed yet'})")
            else:
                memo = f" with memo {q.deposit_memo}" if q.deposit_memo else ""
                say(f"\nSend {intents.fmt_amount(q.amount_in, o.decimals)} {o.symbol} on {intents.chain_label(o.chain)}"
                    + (f" (token {o.contract})" if o.contract else ""))
                say(f"  to   {q.deposit_address}{memo}\n  by   {q.pay_by}  (anything not swapped by then is refunded to {q.request['refundTo']})")
            out = {**q.summary(), **follow(q.deposit_address)} if (buyer_key or args.wait > 0) else q.summary()
            if args.json:
                print(json.dumps(out, indent=1))
            return
        if args.command == "cashout":
            if p["direction"] != "to" or not p["recipient"]:
                sys.exit("cashout 5 to base:usdc <your address there> --shop … --relayer …")
            shop = args.shop or sys.exit("--shop: the shop's NEAR account (pay_to)")
            dst = client.token(p["token"])
            min_out = intents.usd_atomic(p["min"], dst.decimals) if p["min"] else 0
            q = intents.cashout_quote(client, usd=p["usd"], destination=p["token"], recipient=p["recipient"], shop=shop,
                                      min_out=min_out, **limits)
            intents.record(books, q)
            return cashout(intents, client, books, q, args, say, follow)
    except intents.IntentsError as e:
        sys.exit(f"not done: {e}")


def cashout(intents, client, books, q, args, say, follow) -> None:
    shop = args.shop or q.request["refundTo"]
    relayer = args.relayer or sys.exit("--relayer: the account that registers the deposit address and pays the gas")
    shop_key, relayer_key = key("EMPORIUM_NEAR_PAYOUT_KEY"), key("EMPORIUM_NEAR_RELAYER_KEY")
    say(f"Cash out {intents.fmt_amount(q.amount_in, 6)} USDC from {shop} to {q.destination.label}:")
    say("\n".join(intents.quote_lines(q)))
    say(f"  deposit address on NEAR: {q.deposit_address}\n  pay by: {q.pay_by}")
    if not args.yes and input("Type 'send' to cash out: ").strip().lower() != "send":
        sys.exit("Nothing was sent.")
    out = intents.cashout_send(client, q, shop=shop, shop_key=shop_key, relayer=relayer, relayer_key=relayer_key,
                               books=books, rpc_url=args.rpc)
    say(f"Sent: NEAR tx {out['near_tx']} (registration: {out['storage_tx']})")
    out.update(follow(q.deposit_address))
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        for t in out.get("destination_txs") or []:
            say(f"Paid out: {t.get('explorer') or t.get('hash')}")
        say(f"Status: {out['status']}" + ("  (still going: run status again later)" if out.get("timed_out") else ""))


if __name__ == "__main__":
    main()
