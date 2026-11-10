"""Compare near.py's bytes with NEAR's own JavaScript library, field by field: the NEP-366 delegate a buyer
signs, its NEP-461 hash, the signed delegate, and the relayer's transaction (unsigned, its hash, signed).

    npm i --prefix /tmp/nearjs near-api-js@7.4.0
    python scripts/check_borsh.py /tmp/nearjs

near.py has no NEAR SDK under it, so this is the check that its Borsh is NEAR's. Both sides sign with the
same throwaway keys; ed25519 is deterministic, so the signatures must match byte for byte too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import near  # noqa: E402  (near.py has no package-relative imports)

USDC = "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af"


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


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    lib = Path(sys.argv[1]).expanduser().resolve() / "node_modules" / "near-api-js"
    version = json.loads((lib / "package.json").read_text())["version"]
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
    sys.exit(0 if same == len(mine) else 1)


if __name__ == "__main__":
    main()
