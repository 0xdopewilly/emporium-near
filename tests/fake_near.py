"""A NEAR chain in memory, behind NEAR's JSON-RPC, for offline tests and demos.

It keeps accounts (NEAR balance, access keys and their nonces), one NEP-141 token (USDC: balances
and storage registration) and a block height, and it executes what it's sent the way nearcore does:

- a signed transaction is decoded from Borsh and checked: signature over sha256(tx), access key,
  nonce, recent block hash, and the signer pays the gas and any attached deposits;
- a NEP-366 delegate inside it becomes a receipt on the buyer's account, checked again there: its
  own NEP-461 signature, its max_block_height, its key's nonce. Only then does its ft_transfer run;
- the token contract's ft_transfer wants exactly 1 yoctoNEAR, a registered receiver and enough
  balance, and logs the NEP-141 ``ft_transfer`` event.

Outcomes have nearcore's shape, including the trap near.failures() exists for: the delegate's
receipt (and so the transaction's top-level status) succeeds even when the token's receipt fails.

    python tests/fake_near.py 9410     # a chain on http://127.0.0.1:9410, for trying the shop offline
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

try:
    from emporium import near  # under pytest, the plugin is loaded as the package ``emporium``
except ImportError:            # run directly: near.py has no package-relative imports
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import near  # type: ignore[no-redef]

USDC = "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af"   # the testnet USDC account id
STORAGE_DEPOSIT = 1_250_000_000_000_000_000_000                             # 0.00125 NEAR to register
EMPTY_CODE = "11111111111111111111111111111111"
GAS_BURN = 300_000_000_000_000_000_000                                      # ~0.0003 NEAR per relayed payment
BLOCK_NS = 600_000_000                                                      # testnet makes a block every ~0.6 s
NONCE_RANGE = 1_000_000                                                     # nearcore's ACCESS_KEY_NONCE_RANGE_MULTIPLIER


class Refused(Exception):
    """An RPC error, in NEAR's shape: {"name": "HANDLER_ERROR", "cause": {"name": cause}, "data": data}."""

    def __init__(self, cause: str, data: Any = None):
        super().__init__(cause)
        self.cause, self.data = cause, data if data is not None else cause


def _invalid_tx(kind: Any) -> Refused:
    return Refused("INVALID_TRANSACTION", {"TxExecutionError": {"InvalidTxError": kind}})


class _ActionError(Exception):
    def __init__(self, kind: Any):
        super().__init__(json.dumps(kind))
        self.kind = kind


# ---- decoding what the chain is sent -------------------------------------------------------------
def _action(r: "near._Reader", top: bool) -> tuple:
    tag = r.u8()
    if tag == 0:
        return ("CreateAccount",)
    if tag == 2:
        return ("FunctionCall", near.FunctionCall(r.string(256), r.blob(1 << 20), r.u64(), r.u128()))
    if tag == 3:
        return ("Transfer", r.u128())
    if tag == 5:
        pk, _nonce, permission = r.public_key(), r.u64(), r.u8()
        if permission != 1:
            raise ValueError("the fake chain adds full-access keys only")
        return ("AddKey", pk)
    if tag == 8 and top:
        start = r.pos
        sender, receiver = r.account_id(), r.account_id()
        inner = [_action(r, top=False) for _ in range(r.u32())]
        nonce, max_height, pk = r.u64(), r.u64(), r.public_key()
        signed_part = r.data[start:r.pos]
        sig_type, sig = r.signature()
        return ("Delegate", {"sender_id": sender, "receiver_id": receiver, "actions": inner, "nonce": nonce,
                             "max_block_height": max_height, "public_key": pk, "bytes": signed_part,
                             "signature": (sig_type, sig)})
    raise ValueError(f"action type {tag} isn't something the fake chain runs")


def decode_signed_tx(raw: bytes) -> dict:
    r = near._Reader(raw)
    signer, pk, nonce, receiver, block_hash = r.account_id(), r.public_key(), r.u64(), r.account_id(), r.take(32)
    actions = [_action(r, top=True) for _ in range(r.u32())]
    body = raw[:r.pos]
    sig_type, sig = r.signature()
    r.end()
    return {"signer_id": signer, "public_key": pk, "nonce": nonce, "receiver_id": receiver,
            "block_hash": block_hash, "actions": actions, "hash": hashlib.sha256(body).digest(),
            "signature": (sig_type, sig)}


# ---- the chain ---------------------------------------------------------------------------------
class FakeNear:
    def __init__(self, port: int = 0, height: int = 200_000_000, usdc: str = USDC):
        self.lock = threading.RLock()
        self.height, self.usdc_id = height, usdc
        self.t0 = 1_790_000_000_000_000_000 - height * BLOCK_NS
        self.accounts: dict[str, dict] = {}
        self.balances: dict[str, int] = {}
        self.registered: set[str] = set()
        self.outcomes: dict[str, dict] = {}
        self.included: list[dict] = []          # every transaction that made it into a block, decoded
        self.calls: list[tuple[str, dict]] = []
        self._hashes: dict[bytes, int] = {}
        # Things that go wrong on a real network, for tests to switch on:
        self.drop_next = 0        # the RPC answers TIMEOUT_ERROR and the transaction is lost (never included)
        self.timeout_next = 0     # the RPC answers TIMEOUT_ERROR but the transaction does land
        self.fail_transfers = 0   # the token contract panics on the next N ft_transfers
        self.pending_polls = 0    # a FINAL status lookup times out this many times first
        self.rpc_down = False     # every call answers an internal error
        self._add(usdc, 0, code_hash="FakeUSDCcontractCodeHash1111111111111111111")
        self._port = port
        self._httpd: Optional[ThreadingHTTPServer] = None

    # -- set-up --
    def _add(self, account_id: str, amount: int, code_hash: str = EMPTY_CODE) -> dict:
        acct = {"amount": int(amount), "code_hash": code_hash, "keys": {}}
        self.accounts[account_id] = acct
        return acct

    def add_account(self, account_id: str, key: Optional[near.KeyPair] = None, near_amount: float = 0.05,
                    usdc: int = 0, register: bool = True, permission: Any = "FullAccess") -> "FakeNear":
        with self.lock:
            self._add(account_id, int(near_amount * near.NEAR))
            if key is not None:
                self.add_key(account_id, key.public_key, permission)
            if register or usdc:
                self.registered.add(account_id)
            if usdc:
                self.balances[account_id] = int(usdc)
        return self

    def add_key(self, account_id: str, public_key: near.PublicKey, permission: Any = "FullAccess") -> None:
        with self.lock:  # nearcore starts a new key's nonce at (height - 1) * 1e6
            self.accounts[account_id]["keys"][str(public_key)] = {"nonce": (self.height - 1) * NONCE_RANGE,
                                                                  "permission": permission}

    def advance(self, blocks: int = 1) -> int:
        with self.lock:
            self.height += blocks
            return self.height

    def usdc(self, account_id: str) -> int:
        return self.balances.get(account_id, 0)

    def near_balance(self, account_id: str) -> int:
        return self.accounts[account_id]["amount"]

    def key_nonce(self, account_id: str, public_key: Any) -> int:
        return self.accounts[account_id]["keys"][str(public_key)]["nonce"]

    # -- blocks --
    def block_hash(self, height: int) -> bytes:
        h = hashlib.sha256(b"fake-near-block:" + int(height).to_bytes(8, "little")).digest()
        self._hashes[h] = height
        return h

    def header(self, height: int) -> dict:
        return {"height": height, "hash": near.b58encode(self.block_hash(height)),
                "timestamp": self.t0 + height * BLOCK_NS, "prev_hash": near.b58encode(self.block_hash(height - 1))}

    # -- JSON-RPC --
    def handle(self, method: str, params: Any) -> Any:
        with self.lock:
            self.calls.append((method, params))
            if self.rpc_down:
                raise Refused("INTERNAL_ERROR")
            if method == "block":
                if params.get("finality"):
                    return {"header": self.header(self.height), "chunks": []}
                h = int(params.get("block_id"))
                if not 0 < h <= self.height:
                    raise Refused("UNKNOWN_BLOCK")
                return {"header": self.header(h), "chunks": []}
            if method == "query":
                return self._query(params)
            if method == "send_tx":
                return self._send(params)
            if method == "tx":
                return self._status(params)
            raise Refused("METHOD_NOT_FOUND")

    def _account(self, account_id: str) -> dict:
        acct = self.accounts.get(account_id)
        if acct is None:
            raise Refused("UNKNOWN_ACCOUNT", f"account {account_id} does not exist while viewing")
        return acct

    def _query(self, p: dict) -> dict:
        at = {"block_height": self.height, "block_hash": near.b58encode(self.block_hash(self.height))}
        kind = p.get("request_type")
        if kind == "view_account":
            acct = self._account(p["account_id"])
            return {"amount": str(acct["amount"]), "locked": "0", "code_hash": acct["code_hash"],
                    "storage_usage": 182, "storage_paid_at": 0, **at}
        if kind == "view_access_key":
            acct = self._account(p["account_id"])
            key = acct["keys"].get(p["public_key"])
            if key is None:
                raise Refused("UNKNOWN_ACCESS_KEY", f"access key {p['public_key']} does not exist while viewing")
            return {"nonce": key["nonce"], "permission": key["permission"], **at}
        if kind == "call_function":
            if p["account_id"] != self.usdc_id:
                raise Refused("NO_CONTRACT_CODE", f"contract code for {p['account_id']} doesn't exist")
            args = json.loads(base64.b64decode(p.get("args_base64") or "") or b"{}")
            who = str(args.get("account_id"))
            if p["method_name"] == "ft_balance_of":
                value: Any = str(self.balances.get(who, 0))
            elif p["method_name"] == "storage_balance_of":
                value = {"total": str(STORAGE_DEPOSIT), "available": "0"} if who in self.registered else None
            elif p["method_name"] == "ft_metadata":
                value = {"spec": "ft-1.0.0", "name": "USDC", "symbol": "USDC", "decimals": 6}
            else:
                raise Refused("CONTRACT_EXECUTION_ERROR", f"MethodNotFound: {p['method_name']}")
            return {"result": list(json.dumps(value).encode()), "logs": [], **at}
        raise Refused("UNSUPPORTED_REQUEST", str(kind))

    def _status(self, p: dict) -> dict:
        out = self.outcomes.get(p.get("tx_hash"))
        if out is None or out["transaction"]["signer_id"] != p.get("sender_account_id"):
            raise Refused("UNKNOWN_TRANSACTION", f"transaction {p.get('tx_hash')} doesn't exist")
        if self.pending_polls > 0:
            self.pending_polls -= 1
            raise Refused("TIMEOUT_ERROR")
        return out

    def _send(self, p: dict) -> dict:
        raw = base64.b64decode(p.get("signed_tx_base64") or "", validate=True)
        try:
            tx = decode_signed_tx(raw)
        except ValueError as e:
            raise Refused("PARSE_ERROR", f"failed to decode transaction: {e}") from e
        if self.drop_next > 0:
            self.drop_next -= 1
            raise Refused("TIMEOUT_ERROR")
        self._validate(tx)
        out = self._execute(tx)
        if self.timeout_next > 0:
            self.timeout_next -= 1
            raise Refused("TIMEOUT_ERROR")
        if p.get("wait_until") in ("NONE", "INCLUDED", "INCLUDED_FINAL"):
            return {"final_execution_status": p["wait_until"]}
        return out

    # -- what nearcore checks before a transaction gets into a block --
    def _validate(self, tx: dict) -> None:
        signer = self.accounts.get(tx["signer_id"])
        if signer is None:
            raise _invalid_tx({"SignerDoesNotExist": {"signer_id": tx["signer_id"]}})
        key = signer["keys"].get(str(tx["public_key"]))
        if key is None:
            raise _invalid_tx({"InvalidAccessKeyError": {"AccessKeyNotFound": {
                "account_id": tx["signer_id"], "public_key": str(tx["public_key"])}}})
        sig_type, sig = tx["signature"]
        if sig_type != tx["public_key"].key_type or not tx["public_key"].verify(sig, tx["hash"]):
            raise _invalid_tx("InvalidSignature")
        if tx["nonce"] <= key["nonce"]:
            raise _invalid_tx({"InvalidNonce": {"tx_nonce": tx["nonce"], "ak_nonce": key["nonce"]}})
        if tx["nonce"] >= self.height * NONCE_RANGE:
            raise _invalid_tx({"NonceTooLarge": {"tx_nonce": tx["nonce"], "upper_bound": self.height * NONCE_RANGE}})
        made_at = self._hashes.get(tx["block_hash"])
        if made_at is None or self.height - made_at > 86_400:
            raise _invalid_tx("Expired")
        if key["permission"] != "FullAccess" and any(a[0] != "FunctionCall" for a in tx["actions"]):
            raise _invalid_tx({"InvalidAccessKeyError": "RequiresFullAccess"})
        if signer["amount"] < GAS_BURN + self._deposits(tx["actions"]):
            raise _invalid_tx({"NotEnoughBalance": {"signer_id": tx["signer_id"], "balance": str(signer["amount"])}})

    @staticmethod
    def _deposits(actions: list) -> int:
        total = 0
        for a in actions:  # the relayer pays the deposits inside a delegate, as nearcore charges them
            if a[0] == "FunctionCall":
                total += a[1].deposit
            elif a[0] == "Transfer":
                total += a[1]
            elif a[0] == "Delegate":
                total += FakeNear._deposits(a[1]["actions"])
        return total

    # -- running it --
    def _execute(self, tx: dict) -> dict:
        signer = self.accounts[tx["signer_id"]]
        signer["keys"][str(tx["public_key"])]["nonce"] = tx["nonce"]
        signer["amount"] -= GAS_BURN + self._deposits(tx["actions"])
        tx_hash = near.b58encode(tx["hash"])
        self.height += 1  # included
        self.included.append(tx)
        receipts: list[dict] = []

        def receipt(executor: str, status: dict, logs: Optional[list] = None) -> str:
            rid = near.b58encode(hashlib.sha256(f"{tx_hash}:{len(receipts)}".encode()).digest())
            receipts.append({"id": rid, "outcome": {"executor_id": executor, "status": status, "logs": logs or [],
                                                    "receipt_ids": [], "gas_burnt": 2_428_000_000_000}})
            return rid

        top: dict = {"SuccessValue": ""}
        for index, action in enumerate(tx["actions"]):
            self.height += 1
            try:
                logs = self._apply(tx, action, receipt)
                if action[0] != "Delegate":  # a delegate's own receipt was written by _apply
                    receipt(tx["receiver_id"], {"SuccessValue": ""}, logs)
            except _ActionError as e:
                top = {"Failure": {"ActionError": {"index": index, "kind": e.kind}}}
                receipt(tx["receiver_id"], top)
                break
        receipt(tx["signer_id"], {"SuccessValue": ""})  # the gas refund
        out = {
            "final_execution_status": "FINAL",
            "status": top,
            "transaction": {"hash": tx_hash, "signer_id": tx["signer_id"], "receiver_id": tx["receiver_id"],
                            "nonce": tx["nonce"], "public_key": str(tx["public_key"]), "actions": [a[0] for a in tx["actions"]]},
            "transaction_outcome": {"id": tx_hash, "outcome": {"executor_id": tx["signer_id"],
                                                               "status": {"SuccessReceiptId": receipts[0]["id"]},
                                                               "logs": [], "receipt_ids": [receipts[0]["id"]]}},
            "receipts_outcome": receipts,
        }
        self.outcomes[tx_hash] = out
        return out

    def _apply(self, tx: dict, action: tuple, receipt) -> list:
        kind, receiver = action[0], tx["receiver_id"]
        if kind == "Delegate":
            return self._delegate(tx, action[1], receipt)
        if kind == "CreateAccount":
            if receiver in self.accounts:
                raise _ActionError({"AccountAlreadyExists": {"account_id": receiver}})
            self._add(receiver, 0)
            return []
        if kind == "AddKey":
            self.add_key(receiver, action[1])
            return []
        if kind == "Transfer":
            self._account_for_action(receiver)["amount"] += action[1]
            return []
        return self._call(tx["signer_id"], receiver, action[1])

    def _account_for_action(self, account_id: str) -> dict:
        acct = self.accounts.get(account_id)
        if acct is None:
            raise _ActionError({"AccountDoesNotExist": {"account_id": account_id}})
        return acct

    def _delegate(self, tx: dict, d: dict, receipt) -> list:
        """nearcore's apply_delegate_action, then the receipt it makes on the delegate's receiver."""
        sig_type, sig = d["signature"]
        signed = hashlib.sha256(near._u32(near.NEP366_PREFIX) + d["bytes"]).digest()
        if sig_type != d["public_key"].key_type or not d["public_key"].verify(sig, signed):
            raise _ActionError("DelegateActionInvalidSignature")
        if self.height > d["max_block_height"]:
            raise _ActionError("DelegateActionExpired")
        if d["sender_id"] != tx["receiver_id"]:
            raise _ActionError({"DelegateActionSenderDoesNotMatchTxReceiver": {
                "sender_id": d["sender_id"], "receiver_id": tx["receiver_id"]}})
        sender = self._account_for_action(d["sender_id"])
        key = sender["keys"].get(str(d["public_key"]))
        if key is None:
            raise _ActionError({"DelegateActionAccessKeyError": {"AccessKeyNotFound": {
                "account_id": d["sender_id"], "public_key": str(d["public_key"])}}})
        if d["nonce"] <= key["nonce"]:
            raise _ActionError({"DelegateActionInvalidNonce": {"delegate_nonce": d["nonce"], "ak_nonce": key["nonce"]}})
        if d["nonce"] >= self.height * NONCE_RANGE:
            raise _ActionError({"DelegateActionNonceTooLarge": {"delegate_nonce": d["nonce"],
                                                                "upper_bound": self.height * NONCE_RANGE}})
        if key["permission"] != "FullAccess" and any(a[0] != "FunctionCall" or a[1].deposit for a in d["actions"]):
            raise _ActionError({"DelegateActionAccessKeyError": "DepositWithFunctionCall"})
        key["nonce"] = d["nonce"]
        receipt(d["sender_id"], {"SuccessValue": ""})  # the delegate's own receipt: it succeeds from here on
        self.height += 1
        for index, inner in enumerate(d["actions"]):
            try:
                if inner[0] == "FunctionCall":
                    logs = self._call(d["sender_id"], d["receiver_id"], inner[1])
                else:
                    self._account_for_action(d["receiver_id"])["amount"] += inner[1]
                    logs = []
                receipt(d["receiver_id"], {"SuccessValue": ""}, logs)
            except _ActionError as e:
                # The inner receipt fails on its own; the transaction's status still reads success.
                receipt(d["receiver_id"], {"Failure": {"ActionError": {"index": index, "kind": e.kind}}})
                break
        return []

    def _call(self, predecessor: str, contract: str, call: near.FunctionCall) -> list:
        if contract != self.usdc_id:
            raise _ActionError({"FunctionCallError": {"CompilationError": {"CodeDoesNotExist": {"account_id": contract}}}})
        self.accounts[contract]["amount"] += call.deposit

        def panic(msg: str) -> _ActionError:
            return _ActionError({"FunctionCallError": {"ExecutionError": f"Smart contract panicked: {msg}"}})

        try:
            args = json.loads(call.args.decode() or "{}")
        except ValueError:
            raise panic("Failed to deserialize input from JSON.")
        if call.method_name == "storage_deposit":
            if call.deposit < STORAGE_DEPOSIT:
                raise panic("The attached deposit is less than the minimum storage balance")
            self.registered.add(str(args.get("account_id") or predecessor))
            return []
        if call.method_name != "ft_transfer":
            raise _ActionError({"FunctionCallError": {"MethodResolveError": "MethodNotFound"}})
        if call.deposit != 1:
            raise panic("Requires attached deposit of exactly 1 yoctoNEAR")
        receiver, amount = str(args.get("receiver_id")), int(str(args.get("amount")))
        if self.fail_transfers > 0:
            self.fail_transfers -= 1
            raise panic("the fake chain was told to fail this transfer")
        if receiver == predecessor:
            raise panic("Sender and receiver should be different")
        if amount <= 0:
            raise panic("The amount should be a positive number")
        if predecessor not in self.registered:
            raise panic(f"The account {predecessor} is not registered")
        if receiver not in self.registered:
            raise panic(f"The account {receiver} is not registered")
        if self.balances.get(predecessor, 0) < amount:
            raise panic("The account doesn't have enough balance")
        self.balances[predecessor] -= amount
        self.balances[receiver] = self.balances.get(receiver, 0) + amount
        event = {"old_owner_id": predecessor, "new_owner_id": receiver, "amount": str(amount)}
        if args.get("memo"):
            event["memo"] = args["memo"]
        return ["EVENT_JSON:" + json.dumps({"standard": "nep141", "version": "1.0.0", "event": "ft_transfer",
                                            "data": [event]}, separators=(",", ":"))]

    # -- serving --
    def start(self) -> "FakeNear":
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                try:
                    reply = {"jsonrpc": "2.0", "id": body.get("id"), "result": outer.handle(body.get("method"), body.get("params") or {})}
                except Refused as e:
                    reply = {"jsonrpc": "2.0", "id": body.get("id"),
                             "error": {"name": "HANDLER_ERROR", "cause": {"name": e.cause, "info": {}}, "code": -32000,
                                       "message": "Server error", "data": e.data}}
                b = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self._port), H)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._httpd is not None, "start() the chain first"
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


if __name__ == "__main__":
    chain = FakeNear(int(sys.argv[1]) if len(sys.argv) > 1 else 9410).start()
    print(f"fake NEAR chain on {chain.url} (USDC at {USDC}); Ctrl-C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        chain.stop()
