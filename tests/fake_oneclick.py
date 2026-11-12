"""A stand-in for NEAR Intents' 1Click API, for offline tests: tokens, quotes it signs with its own
test key (the way 1Click signs, via intents.signed_quote_json), deposit notices and status.

Status for a deposit address walks a script (``statuses[deposit]``, one step per look) or, for a
cash-out whose deposit address lives on a FakeNear chain, follows the chain: PENDING_DEPOSIT until the
address holds the quoted USDC, then PROCESSING, then SUCCESS.

Knobs: ``tamper(resp)`` edits a signed response (to forge a field), ``unsigned`` sends a bad signature,
``errors`` is a queue of (HTTP status, body) answered first, ``min_amount`` refuses amounts below it
the way 1Click does ("Amount is too low for bridge, try at least N").
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from emporium import intents, near

TOKENS = json.loads((Path(__file__).parent / "fixtures" / "oneclick_tokens.json").read_text())
FEE_BPS = 10  # what the fake charges per swap


class FakeOneClick:
    def __init__(self, key: near.KeyPair, chain: Any = None, tokens: Optional[list] = None):
        self.key, self.chain = key, chain
        self.tokens = tokens if tokens is not None else TOKENS
        self.quotes: dict[str, dict] = {}            # deposit address -> signed quote response
        self.statuses: dict[str, list[str]] = {}     # deposit address -> statuses still to show
        self.submitted: list[dict] = []
        self.requests: list[tuple[str, str, Any]] = []
        self.tamper: Optional[Callable[[dict], dict]] = None
        self.unsigned = False
        self.errors: list[tuple[int, Any]] = []
        self.min_amount: Optional[int] = None
        self.time_estimate = 22
        self._n = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, obj: Any) -> None:
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _route(self, method: str) -> None:
                url = urlparse(self.path)
                body = None
                if method == "POST":
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append((method, url.path, body if body is not None else parse_qs(url.query),
                                       self.headers.get("Authorization")))
                if outer.errors:
                    code, err = outer.errors.pop(0)
                    return self._send(code, err)
                code, obj = outer.handle(method, url.path, body, {k: v[0] for k, v in parse_qs(url.query).items()})
                self._send(code, obj)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v0"

    def start(self) -> "FakeOneClick":
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- the API --
    def handle(self, method: str, path: str, body: Any, query: dict) -> tuple[int, Any]:
        if method == "GET" and path == "/v0/tokens":
            return 200, self.tokens
        if method == "POST" and path == "/v0/quote":
            return self.quote(body or {})
        if method == "GET" and path == "/v0/status":
            return self.status(query.get("depositAddress", ""))
        if method == "POST" and path == "/v0/deposit/submit":
            self.submitted.append(body)
            if body.get("depositAddress") not in self.quotes:
                return 400, {"message": "deposit address not found"}
            return self.status(body["depositAddress"], peek=True)
        return 404, {"message": "Cannot " + method + " " + path}

    def _token(self, asset: str) -> Optional[dict]:
        return next((t for t in self.tokens if t["assetId"] == asset), None)

    def _deposit_address(self, chain: str) -> str:
        self._n += 1
        h = hashlib.sha256(f"fake-1click-deposit-{self._n}-{time.time()}".encode()).digest()
        if chain == "near":
            return h.hex()
        if chain == "sol":
            return near.b58encode(h)
        return "0x" + h[:20].hex()

    def quote(self, req: dict) -> tuple[int, Any]:
        src, dst = self._token(req.get("originAsset", "")), self._token(req.get("destinationAsset", ""))
        if not src or not dst:
            return 400, {"message": "tokenIn is not valid"}
        if not req.get("refundTo") or not req.get("recipient"):
            return 400, {"message": "refundTo is not valid"}
        amount = int(req["amount"])
        if self.min_amount is not None and amount < self.min_amount:
            return 400, {"message": f"Amount is too low for bridge, try at least {self.min_amount} "}
        p_in, p_out = Decimal(str(src["price"])), Decimal(str(dst["price"]))
        slip = Decimal(int(req.get("slippageTolerance", 100))) / 10_000
        fee = Decimal(FEE_BPS) / 10_000
        d_in, d_out = Decimal(10) ** src["decimals"], Decimal(10) ** dst["decimals"]
        if req["swapType"] == "EXACT_OUTPUT":
            out_usd = amount / d_out * p_out
            min_in = math.ceil(out_usd * (1 + fee) / p_in * d_in)
            amount_in = math.ceil(min_in * (1 + slip))
            amount_out = min_out = amount
        else:
            amount_in = min_in = amount
            amount_out = math.floor(amount / d_in * p_in * (1 - fee) / p_out * d_out)
            min_out = math.floor(amount_out * (1 - slip))
        q = {"amountIn": str(amount_in), "amountInFormatted": intents.fmt_amount(amount_in, src["decimals"]),
             "amountInUsd": str(amount_in / d_in * p_in), "minAmountIn": str(min_in),
             "amountOut": str(amount_out), "amountOutFormatted": intents.fmt_amount(amount_out, dst["decimals"]),
             "amountOutUsd": str(amount_out / d_out * p_out), "minAmountOut": str(min_out),
             "timeEstimate": self.time_estimate, "refundFee": "0", "withdrawFee": "0"}
        echoed = {**req, "depositMode": "SIMPLE", "confidentiality": "public", "quoteWaitingTimeMs": 0, "insured": False,
                  "appFees": [{"recipient": "f" * 64, "fee": 1}]}
        if not req.get("dry"):
            deadline = intents.parse_iso(req["deadline"]) or time.time()
            q.update({"deadline": intents.iso(deadline + 72 * 3600), "timeWhenInactive": intents.iso(deadline + 72 * 3600),
                      "depositAddress": self._deposit_address(src["blockchain"])})
        resp = {"quote": q, "quoteRequest": echoed, "timestamp": intents.iso(time.time()),
                "correlationId": f"fake-{self._n}"}
        resp["signature"] = "ed25519:" + near.b58encode(self.key.sign(intents.quote_message(intents.signed_quote_json(resp))))
        if self.unsigned:
            resp["signature"] = "ed25519:" + near.b58encode(bytes(64))
        if self.tamper:
            resp = self.tamper(json.loads(json.dumps(resp)))
        if not req.get("dry"):
            self.quotes[q["depositAddress"]] = resp
        return 201, resp

    def status(self, deposit: str, peek: bool = False) -> tuple[int, Any]:
        resp = self.quotes.get(deposit)
        if resp is None:
            return 404, {"message": f"Deposit address {deposit} not found", "statusCode": 404}
        script = self.statuses.get(deposit)
        if script:
            state = script[0] if peek or len(script) == 1 else script.pop(0)
        elif self.chain is not None and resp["quoteRequest"]["originAsset"] == intents.NEAR_USDC:
            held = self.chain.usdc(deposit)
            seen = self.statuses.setdefault("_seen:" + deposit, [])
            if held < int(resp["quote"]["amountIn"]):
                state = "PENDING_DEPOSIT"
            elif not seen:
                state = "PROCESSING"
                if not peek:
                    seen.append("PROCESSING")
            else:
                state = "SUCCESS"
        else:
            state = "PENDING_DEPOSIT"
        details: dict[str, Any] = {"intentHashes": [], "nearTxHashes": [], "originChainTxHashes": [],
                                   "destinationChainTxHashes": [], "refundedAmount": "0", "refundedAmountFormatted": "0"}
        if state == "SUCCESS":
            details.update({"amountOut": resp["quote"]["amountOut"], "amountOutFormatted": resp["quote"]["amountOutFormatted"],
                            "destinationChainTxHashes": [{"hash": "0x" + hashlib.sha256(deposit.encode()).hexdigest(),
                                                          "explorerUrl": "https://basescan.org/tx/0x" + hashlib.sha256(deposit.encode()).hexdigest()}]})
        if state == "REFUNDED":
            details.update({"refundedAmount": resp["quote"]["amountIn"], "refundedAmountFormatted": resp["quote"]["amountInFormatted"],
                            "refundReason": "DEADLINE_EXCEEDED"})
        return 200, {"correlationId": "fake-status", "quoteResponse": resp, "status": state,
                     "updatedAt": intents.iso(time.time()), "swapDetails": details}
