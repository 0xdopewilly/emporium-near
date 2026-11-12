"""Compare near.py's bytes with NEAR's own JavaScript library, field by field: the NEP-366 delegate a buyer
signs, its NEP-461 hash, the signed delegate, and the relayer's transaction (unsigned, its hash, signed).

    npm i --prefix /tmp/nearjs near-api-js@7.4.0
    python scripts/check_borsh.py /tmp/nearjs
    python scripts/check_borsh.py /tmp/nearjs --fuzz 10000 [--seed 1] [--json bench/results/borsh-fuzz.json]

near.py has no NEAR SDK under it, so this is the check that its Borsh is NEAR's. Both sides sign with the
same throwaway keys; ed25519 is deterministic, so the signatures must match byte for byte too.

--fuzz N adds N random cases on top of the fixed one: named and implicit (64-hex, 0x-hex) account ids of
every length, 1-4 function calls per delegate, gas and nonces across the u64 range, deposits across u128,
block heights, method names, JSON arguments with unicode, escapes and nesting, and raw argument bytes. For
each, both sides encode the delegate, hash it, sign it and wrap it in a relayer transaction; near.py must
also read near-api-js's signed delegate back to the same bytes. (interop/node_modules holds near-api-js
7.4.0 too, so ``python scripts/check_borsh.py interop --fuzz 10000`` works after ``cd interop && npm ci``.)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import near  # noqa: E402  (near.py has no package-relative imports)

USDC = "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af"
FIELDS = ("delegate_action_b64", "nep461_hash_hex", "signed_delegate_b64", "tx_b64", "tx_hash_b58", "signed_tx_b64")


def seeded(text: str) -> near.KeyPair:
    return near.KeyPair("ed25519:" + near.b58encode(hashlib.sha256(text.encode()).digest()))


def ours(b: near.KeyPair, r: near.KeyPair) -> dict:
    """The same delegate and relayer transaction the oracle builds, encoded by near.py."""
    args = json.dumps({"receiver_id": "ada-shop.testnet", "amount": "50000"}, separators=(",", ":")).encode()
    da = near.DelegateAction("buyer-1.testnet", USDC, (near.FunctionCall("ft_transfer", args, 30 * near.TGAS, 1),),
                             123456789012345, 277045412, b.public_key)
    sda = da.sign(b)
    tx = near.Transaction("relayer-1.testnet", r.public_key, 99, "buyer-1.testnet", bytes([7]) * 32, (near.Delegate(sda),))
    tx_hash, signed = tx.sign(r)
    return {
        "buyer_public_key": str(b.public_key),
        "delegate_action_b64": base64.b64encode(near._u32(near.NEP366_PREFIX) + da.to_borsh()).decode(),
        "nep461_hash_hex": da.signing_hash().hex(),
        "signed_delegate_b64": sda.to_base64(),
        "tx_b64": base64.b64encode(tx.to_borsh()).decode(),
        "tx_hash_b58": tx_hash,
        "signed_tx_b64": signed,
    }


def fixed(lib: Path, version: str) -> bool:
    b, r = seeded("emporium-near-test-buyer"), seeded("emporium-near-test-relayer")
    run = subprocess.run(["node", str(ROOT / "tests/fixtures/near_oracle.mjs"), b.secret(), r.secret()],
                         capture_output=True, text=True, env={**os.environ, "NEARJS": str(lib / "lib" / "index.js")})
    if run.returncode:
        sys.exit(run.stderr.strip() or "the oracle failed")
    theirs, mine = json.loads(run.stdout), ours(b, r)
    print(f"near.py vs near-api-js {version}, same inputs and keys:")
    same = 0
    for field, value in mine.items():
        ok = theirs.get(field) == value
        same += ok
        size = len(base64.b64decode(value)) if field.endswith("_b64") else len(value)
        print(f"  {'✓' if ok else '✗'} {field:<22} {size:>4} {'bytes' if field.endswith('_b64') else 'chars'}")
    print(f"{same} of {len(mine)} identical")
    return same == len(mine)


# ---- fuzz --------------------------------------------------------------------------------------
U64, U128 = 2**64 - 1, 2**128 - 1
_SAFE = 2**53 - 1  # JSON numbers both sides print the same way
_TEXT = (  # ASCII, quotes and escapes, controls, Latin-1, Greek/CJK/Hangul, RTL, emoji, separators and BOM, astral
    "abcxyz ABC 0189 _-.:/",
    "\"\\",
    "\x00\x01\x08\x09\x0a\x0c\x0d\x1f\x7f",
    "\xe9\xfc\xdf\xf1\xe7\xf8\xe5",
    "\u03a9\u03c0\u03bb\u4e2d\u6587\u6f22\u5b57\u65e5\u672c\u8a9e\ud55c\uad6d\uc5b4",
    "\u0639\u0631\u0628\u0649\u05e2\u05d1\u05e8\u05d9\u05ea",
    "\U0001f600\U0001f680\U0001f980\U0001f4b8\U0001f1f3\U0001f1ec",
    "\u2028\u2029\ufeff\ufffd\uffff",
    "\U0001d11e\U00010348",
)


def _text(rng: random.Random, n: int) -> str:
    pools = [rng.choice(_TEXT) for _ in range(3)]
    return "".join(rng.choice(rng.choice(pools)) for _ in range(n))


def _key(rng: random.Random) -> str:
    return rng.choice("abcdefghijklmnopqrstuvwxyzé漢😀_") + _text(rng, rng.randint(0, 10))


def _json(rng: random.Random, depth: int = 0):
    kind = rng.random()
    if depth > 2 or kind < 0.35:
        return rng.choice([lambda: _text(rng, rng.randint(0, 40)), lambda: rng.randint(-_SAFE, _SAFE),
                           lambda: rng.randint(0, 1000), lambda: rng.choice([True, False, None]),
                           lambda: str(rng.randint(0, U128))])()
    if kind < 0.6:
        return [_json(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {_key(rng): _json(rng, depth + 1) for _ in range(rng.randint(0, 5))}


def _args(rng: random.Random) -> dict:
    """An ft_transfer-like object, sometimes with a memo, sometimes anything at all."""
    if rng.random() < 0.4:
        a = {"receiver_id": _account(rng), "amount": str(rng.randint(0, U128))}
        if rng.random() < 0.5:
            a["memo"] = _text(rng, rng.randint(0, 60))
        return a
    return {_key(rng): _json(rng) for _ in range(rng.randint(0, 6))}


def _account(rng: random.Random) -> str:
    kind = rng.random()
    if kind < 0.2:
        return "".join(rng.choice("0123456789abcdef") for _ in range(64))   # implicit
    if kind < 0.3:
        return "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(40))  # eth-implicit
    while True:
        target = rng.choice([2, 3, rng.randint(2, 64), 64, rng.randint(5, 30)])
        parts, size = [], 0
        while size < target:
            seg = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(rng.randint(1, 12)))
            parts.append(seg)
            size += len(seg) + 1
        out = ""
        for i, p in enumerate(parts):
            out += (rng.choice(".-_") if i else "") + p
        out = out[:target].strip(".-_")
        if near.is_account_id(out):
            return out


def _kind(account: str) -> str:
    if len(account) == 64 and all(c in "0123456789abcdef" for c in account):
        return "implicit"
    if len(account) == 42 and account.startswith("0x") and all(c in "0123456789abcdef" for c in account[2:]):
        return "eth-implicit"
    return "named"


def _int(rng: random.Random, top: int) -> int:
    return rng.choice([0, 1, top, top - 1, rng.randint(0, top), rng.randint(0, 2**32), rng.randint(0, 10**6)])


def _case(rng: random.Random) -> tuple[dict, dict]:
    buyer = near.KeyPair("ed25519:" + near.b58encode(rng.randbytes(32)))
    relayer = near.KeyPair("ed25519:" + near.b58encode(rng.randbytes(32)))
    calls, specs = [], []
    for _ in range(rng.choice([1, 1, 1, 2, 3, 4])):
        method = rng.choice(["ft_transfer", "ft_transfer_call", "storage_deposit", "", _key(rng)[:40],
                             "".join(rng.choice("abcdefghijklmnopqrstuvwxyz_") for _ in range(rng.randint(1, 64)))])
        gas, deposit = _int(rng, U64), _int(rng, U128)
        if rng.random() < 0.8:
            obj = _args(rng)
            raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
            specs.append({"method": method, "json": obj, "raw": None, "gas": str(gas), "deposit": str(deposit)})
        else:
            raw = rng.randbytes(rng.randint(0, 300))
            specs.append({"method": method, "json": None, "raw": base64.b64encode(raw).decode(), "gas": str(gas),
                          "deposit": str(deposit)})
        calls.append(near.FunctionCall(method, raw, gas, deposit))
    sender, receiver = _account(rng), rng.choice([USDC, _account(rng)])
    nonce, mbh, tx_nonce = _int(rng, U64), _int(rng, U64), _int(rng, U64)
    relayer_id, block_hash = _account(rng), rng.randbytes(32)
    tx_receiver = rng.choice([sender, _account(rng)])
    da = near.DelegateAction(sender, receiver, tuple(calls), nonce, mbh, buyer.public_key)
    sda = da.sign(buyer)
    tx = near.Transaction(relayer_id, relayer.public_key, tx_nonce, tx_receiver, block_hash, (near.Delegate(sda),))
    tx_hash, signed = tx.sign(relayer)
    mine = {
        "delegate_action_b64": base64.b64encode(near._u32(near.NEP366_PREFIX) + da.to_borsh()).decode(),
        "nep461_hash_hex": da.signing_hash().hex(),
        "signed_delegate_b64": sda.to_base64(),
        "tx_b64": base64.b64encode(tx.to_borsh()).decode(),
        "tx_hash_b58": tx_hash,
        "signed_tx_b64": signed,
    }
    spec = {"buyer": buyer.secret(), "relayer": relayer.secret(), "sender": sender, "receiver": receiver,
            "nonce": str(nonce), "mbh": str(mbh), "actions": specs, "relayer_id": relayer_id,
            "tx_receiver": tx_receiver, "tx_nonce": str(tx_nonce), "block_hash": base64.b64encode(block_hash).decode()}
    return spec, mine


def fuzz(lib: Path, version: str, n: int, seed: int, report: str | None) -> bool:
    rng = random.Random(seed)
    t0 = time.time()
    cases = [_case(rng) for _ in range(n)]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump([spec for spec, _ in cases], f, ensure_ascii=False)
        path = f.name
    try:
        run = subprocess.run(["node", str(ROOT / "tests/fixtures/near_fuzz_oracle.mjs"), path], capture_output=True,
                             text=True, env={**os.environ, "NEARJS": str(lib / "lib" / "index.js")})
    finally:
        os.unlink(path)
    if run.returncode:
        sys.exit(run.stderr.strip()[-2000:] or "the fuzz oracle failed")
    theirs = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
    if len(theirs) != n:
        sys.exit(f"the fuzz oracle answered {len(theirs)} of {n} cases")
    per_field = {f: 0 for f in FIELDS}
    per_field["decode_roundtrip"] = 0
    identical, bad = 0, []
    for k, ((spec, mine), their) in enumerate(zip(cases, theirs)):
        ok = True
        for f in FIELDS:
            same = mine[f] == their[f]
            per_field[f] += same
            ok &= same
        try:  # near.py reads near-api-js's signed delegate back to the same bytes
            raw = base64.b64decode(their["signed_delegate_b64"])
            back = near.SignedDelegateAction.from_borsh(raw).to_borsh() == raw
        except Exception:
            back = False
        per_field["decode_roundtrip"] += back
        ok &= back
        identical += ok
        if not ok and len(bad) < 5:
            bad.append({"case": k, "spec": {x: spec[x] for x in spec if x not in ("buyer", "relayer")},
                        "differs": [f for f in FIELDS if mine[f] != their[f]] + ([] if back else ["decode_roundtrip"])})
    actions = [len(spec["actions"]) for spec, _ in cases]
    coverage = {
        "implicit_senders": sum(_kind(s["sender"]) == "implicit" for s, _ in cases),
        "eth_implicit_senders": sum(_kind(s["sender"]) == "eth-implicit" for s, _ in cases),
        "named_senders": sum(_kind(s["sender"]) == "named" for s, _ in cases),
        "function_calls": sum(actions),
        "multi_action_delegates": sum(a > 1 for a in actions),
        "json_args": sum(a["json"] is not None for s, _ in cases for a in s["actions"]),
        "raw_args": sum(a["raw"] is not None for s, _ in cases for a in s["actions"]),
        "non_ascii_json_args": sum(a["json"] is not None and not json.dumps(a["json"], ensure_ascii=False).isascii()
                                   for s, _ in cases for a in s["actions"]),
        "max_deposit_u128": sum(int(a["deposit"]) == U128 for s, _ in cases for a in s["actions"]),
        "max_gas_u64": sum(int(a["gas"]) == U64 for s, _ in cases for a in s["actions"]),
        "max_nonce_u64": sum(int(s["nonce"]) == U64 for s, _ in cases),
    }
    secs = time.time() - t0
    print(f"near.py vs near-api-js {version}, {n} random delegates, signed delegates and relayer transactions (seed {seed}):")
    for f, same in per_field.items():
        print(f"  {'✓' if same == n else '✗'} {f:<22} {same}/{n}")
    print(f"{identical} of {n} identical")
    if bad:
        print("first differences:", json.dumps(bad, ensure_ascii=False)[:1500])
    if report:
        out = Path(report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "oracle": f"near-api-js {version}",
            "cases": n, "seed": seed, "identical": identical, "perField": per_field, "coverage": coverage,
            "seconds": round(secs, 1), "firstDifferences": bad,
            "repro": f"python scripts/check_borsh.py <dir with near-api-js@{version}> --fuzz {n} --seed {seed}",
        }, indent=1, ensure_ascii=False))
        print(f"wrote {report}")
    return identical == n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nearjs", help="a folder whose node_modules holds near-api-js")
    ap.add_argument("--fuzz", type=int, default=0, metavar="N", help="also compare N random cases")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--json", metavar="PATH", help="write the fuzz results here")
    a = ap.parse_args()
    lib = Path(a.nearjs).expanduser().resolve() / "node_modules" / "near-api-js"
    version = json.loads((lib / "package.json").read_text())["version"]
    ok = fixed(lib, version)
    if a.fuzz > 0:
        ok = fuzz(lib, version, a.fuzz, a.seed, a.json) and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
