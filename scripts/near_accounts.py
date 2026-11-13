"""Make (or check) the NEAR testnet accounts for a pair of demo shops, all under one parent account the
public faucet funds. Uses only this plugin's own near.py.

    python scripts/near_accounts.py setup [--parent emporium.testnet] [--buyers 5]   # create what's missing
    python scripts/near_accounts.py status                                           # NEAR and USDC balances
    python scripts/near_accounts.py fund <account> <usd>                             # send test USDC from the parent
    python scripts/near_accounts.py env <account>                                    # print KEY=… lines for a .env
    python scripts/near_accounts.py fleet .dev/fleet-near.json                       # the buyers, as a bench wallet file
    python scripts/near_accounts.py create demo-buyer [--near 0.03]                  # one more account: <name>.<parent>

Keys are written to .dev/near-wallets.json (git-ignored, mode 600; ``--wallets`` names another file) and are
never printed except by ``env``, which you redirect into a profile's .env yourself. The layout:

    <parent>                  the faucet's NEAR, and the test USDC float (from https://faucet.circle.com, NEAR Testnet)
    ada.<parent>              Ada's earnings (pay_to). Its key never needs to be on a server.
    ada-relay.<parent>        submits buyers' signed transfers to Ada and pays their gas (key on Ada's server)
    ada-wallet.<parent>       what Ada's shop pays Tunde from when a job subcontracts (key on Ada's server)
    tunde.<parent>, tunde-relay.<parent>
    buyer-1 … buyer-N.<parent>   demo buyers: they hold USDC and no NEAR beyond the account minimum
    demo-buyer.<parent>          (create demo-buyer) pays for a shop's keyless demo: demo_buyer_account
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import near  # noqa: E402  (near.py has no package-relative imports)

USDC = "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af"   # Circle's USDC on NEAR testnet
FAUCET = "https://helper.nearprotocol.com/account"
WALLETS = ROOT / ".dev" / "near-wallets.json"
STORAGE_DEPOSIT = 1_250_000_000_000_000_000_000                             # 0.00125 NEAR: USDC's registration fee
ROLES = {"ada": "0.05", "ada-relay": "1", "ada-wallet": "0.1", "tunde": "0.05", "tunde-relay": "1"}
BUYER_NEAR = "0.03"

rpc = near.Rpc(os.environ.get("NEAR_RPC", "https://test.rpc.fastnear.com"))


def load() -> dict:
    return json.loads(WALLETS.read_text()) if WALLETS.exists() else {}


def save(w: dict) -> None:
    WALLETS.parent.mkdir(exist_ok=True)
    tmp = WALLETS.with_suffix(".tmp")
    tmp.write_text(json.dumps(w, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(WALLETS)


def yocto(n: str) -> int:
    return int(Decimal(n) * near.NEAR)


def key_of(w: dict, account: str) -> near.KeyPair:
    return near.KeyPair(w[account]["secret"])


def usdc(account: str) -> Decimal:
    try:
        return Decimal(int(rpc.view(USDC, "ft_balance_of", {"account_id": account}))) / 10**6
    except Exception:
        return Decimal(0)


def registered(account: str) -> bool:
    return rpc.view(USDC, "storage_balance_of", {"account_id": account}) is not None


def ensure_parent(w: dict, parent: str) -> None:
    acct = rpc.account(parent)
    if acct is not None:
        if parent not in w:
            sys.exit(f"{parent} exists but its key isn't in {WALLETS}; pick another --parent")
        return
    kp = near.KeyPair.generate()
    w[parent] = {"secret": kp.secret(), "public": str(kp.public_key), "role": "parent"}
    save(w)  # keep the key before asking, so a slow faucet can't lose it
    r = httpx.post(FAUCET, json={"newAccountId": parent, "newAccountPublicKey": str(kp.public_key)}, timeout=60)
    if r.status_code >= 300:
        sys.exit(f"the faucet refused {parent} (HTTP {r.status_code}): {r.text[:200]}")
    for _ in range(30):
        if rpc.account(parent):
            break
        time.sleep(2)
    print(f"created {parent} from the faucet")


def ensure_sub(w: dict, parent: str, account: str, fund: str, role: str) -> None:
    if rpc.account(account) is not None:
        if account not in w:
            sys.exit(f"{account} exists but its key isn't in {WALLETS}")
        return
    kp = near.KeyPair(w[account]["secret"]) if account in w else near.KeyPair.generate()
    w[account] = {"secret": kp.secret(), "public": str(kp.public_key), "role": role}
    save(w)
    out = near.transact(rpc, parent, key_of(w, parent), account,
                        (near.CreateAccount(), near.Transfer(yocto(fund)), near.AddFullAccessKey(kp.public_key)))
    if near.failures(out):
        sys.exit(f"creating {account} failed: {json.dumps(near.failures(out))[:300]}")
    print(f"created {account} with {fund} NEAR")


def ensure_registered(w: dict, parent: str, account: str) -> None:
    if registered(account):
        return
    args = json.dumps({"account_id": account, "registration_only": True}).encode()
    out = near.transact(rpc, parent, key_of(w, parent), USDC,
                        (near.FunctionCall("storage_deposit", args, 30 * near.TGAS, STORAGE_DEPOSIT),))
    if near.failures(out):
        sys.exit(f"registering {account} with USDC failed: {json.dumps(near.failures(out))[:300]}")
    print(f"registered {account} with USDC")


def accounts(parent: str, buyers: int) -> list[tuple[str, str, str]]:
    out = [(f"{name}.{parent}", fund, name) for name, fund in ROLES.items()]
    out += [(f"buyer-{i}.{parent}", BUYER_NEAR, "buyer") for i in range(1, buyers + 1)]
    return out


def cmd_setup(a) -> None:
    w = load()
    ensure_parent(w, a.parent)
    for account, fund, role in accounts(a.parent, a.buyers):
        ensure_sub(w, a.parent, account, fund, role)
    w.setdefault("_meta", {}).update({"parent": a.parent, "buyers": a.buyers})
    save(w)
    for account, _, role in accounts(a.parent, a.buyers):
        if not role.endswith("relay"):  # relayers only pay gas; everyone else holds or receives USDC
            ensure_registered(w, a.parent, account)
    ensure_registered(w, a.parent, a.parent)
    cmd_status(a)


def cmd_status(a) -> None:
    w = load()
    parent = w.get("_meta", {}).get("parent") or a.parent
    rows = [(parent, "parent")] + [(acc, role) for acc, _, role in accounts(parent, w.get("_meta", {}).get("buyers", a.buyers))]
    for account, role in rows:
        info = rpc.account(account)
        if info is None:
            print(f"{account:44} {role:12} (not created)")
            continue
        nearbal = Decimal(int(info["amount"])) / near.NEAR
        print(f"{account:44} {role:12} {nearbal:>10.4f} NEAR  {usdc(account):>9.2f} USDC"
              f"{'' if role.endswith('relay') or registered(account) else '  (not registered with USDC)'}")


def cmd_fund(a) -> None:
    w = load()
    frm = a.from_account or w.get("_meta", {}).get("parent")
    if frm not in w:
        sys.exit(f"no key for {frm}")
    amount = int(Decimal(a.usd) * 10**6)
    args = json.dumps({"receiver_id": a.account, "amount": str(amount)}).encode()
    out = near.transact(rpc, frm, key_of(w, frm), USDC, (near.FunctionCall("ft_transfer", args, 30 * near.TGAS, 1),))
    if near.failures(out):
        sys.exit(f"transfer failed: {json.dumps(near.failures(out))[:300]}")
    print(f"sent {a.usd} USDC from {frm} to {a.account}: https://testnet.nearblocks.io/txns/{out['transaction']['hash']}")


def cmd_env(a) -> None:
    w = load()
    if a.account not in w:
        sys.exit(f"no key for {a.account}")
    print(f"{a.var}={w[a.account]['secret']}")


def cmd_create(a) -> None:
    """One more account under the parent, registered with USDC (e.g. the keyless demo's paying account)."""
    w = load()
    parent = a.parent or w.get("_meta", {}).get("parent") or "emporium.testnet"
    if parent not in w:
        sys.exit(f"no key for the parent {parent} in {WALLETS}")
    account = a.name if a.name.endswith("." + parent) else f"{a.name}.{parent}"
    if not near.is_account_id(account):
        sys.exit(f"{account} is not a NEAR account id")
    ensure_sub(w, parent, account, a.near, a.role)
    ensure_registered(w, parent, account)
    info = rpc.account(account) or {}
    print(f"{account}: {Decimal(int(info.get('amount', 0))) / near.NEAR:.4f} NEAR, {usdc(account):.2f} USDC, registered with USDC "
          f"(key in {WALLETS})")


def cmd_fleet(a) -> None:
    """Write the buyer accounts and keys as a wallet file for bench/stress.py and bench/redteam.py."""
    w = load()
    buyers = [{"account": acc, "key": w[acc]["secret"]} for acc in sorted(w, key=lambda x: (len(x), x))
              if acc.startswith("buyer-") and "secret" in w[acc]]
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"buyers": buyers}, indent=1))
    os.chmod(out, 0o600)
    print(f"wrote {len(buyers)} buyers to {out} (keys inside: keep it out of git)")


def main() -> None:
    global WALLETS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wallets", default=str(WALLETS), help="the wallet file (default .dev/near-wallets.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--parent", default="emporium.testnet")
    s.add_argument("--buyers", type=int, default=5)
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("status")
    s.add_argument("--parent", default="emporium.testnet")
    s.add_argument("--buyers", type=int, default=5)
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("fund")
    s.add_argument("account")
    s.add_argument("usd")
    s.add_argument("--from", dest="from_account", default="")
    s.set_defaults(fn=cmd_fund)
    s = sub.add_parser("env")
    s.add_argument("account")
    s.add_argument("--var", default="EMPORIUM_NEAR_RELAYER_KEY")
    s.set_defaults(fn=cmd_env)
    s = sub.add_parser("fleet")
    s.add_argument("out")
    s.set_defaults(fn=cmd_fleet)
    s = sub.add_parser("create")
    s.add_argument("name", help="e.g. demo-buyer, for demo-buyer.<parent>")
    s.add_argument("--parent", default="")
    s.add_argument("--near", default="0.03", help="NEAR to create it with (it needs none for USDC payments)")
    s.add_argument("--role", default="demo-buyer")
    s.set_defaults(fn=cmd_create)
    a = ap.parse_args()
    WALLETS = Path(a.wallets).expanduser().resolve()
    a.fn(a)


if __name__ == "__main__":
    main()
