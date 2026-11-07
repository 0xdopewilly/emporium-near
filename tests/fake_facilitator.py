"""A stand-in x402 facilitator for offline tests and demos: says yes to every well-formed payment.

    python tests/fake_facilitator.py 9409      # then set facilitator_url: http://127.0.0.1:9409

It checks nothing on-chain; it only exists so the shop's flow can be exercised without a funded wallet.
Pass ``--fail-settle`` to make every settlement fail (the "work done, payment fails" path).
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NETWORKS = ("eip155:84532", "eip155:8453")


class FakeFacilitator:
    def __init__(self, port: int = 0, fail_settle: bool | int = False, invalid: bool = False):
        # fail_settle: True fails every settlement; an int N fails only the first N (a flaky facilitator).
        self.fail_settle, self.invalid = fail_settle, invalid
        self.settle_reason = "invalid_exact_evm_transaction_failed"
        self.verified: list[dict] = []
        self.settled: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/supported"):
                    return self._json(200, {"kinds": [{"x402Version": 2, "scheme": "exact", "network": n} for n in NETWORKS],
                                            "extensions": [], "signers": {"eip155:*": ["0x" + "fa" * 20]}})
                self._json(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                payload = body.get("paymentPayload") or {}
                payer = ((payload.get("payload") or {}).get("authorization") or {}).get("from", "")
                if self.path.rstrip("/").endswith("/verify"):
                    outer.verified.append(body)
                    if outer.invalid:
                        return self._json(200, {"isValid": False, "invalidReason": "invalid_exact_evm_insufficient_balance", "payer": payer})
                    return self._json(200, {"isValid": True, "payer": payer})
                if self.path.rstrip("/").endswith("/settle"):
                    outer.settled.append(body)
                    net = (payload.get("accepted") or {}).get("network", NETWORKS[0])
                    fail = outer.fail_settle
                    if fail is True or (type(fail) is int and len(outer.settled) <= fail):
                        return self._json(200, {"success": False, "errorReason": outer.settle_reason, "transaction": "",
                                                "network": net, "payer": payer})
                    return self._json(200, {"success": True, "transaction": "0x" + secrets.token_hex(32), "network": net, "payer": payer})
                self._json(404, {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeFacilitator":
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    port = int(next((a for a in sys.argv[1:] if a.isdigit()), "9409"))
    f = FakeFacilitator(port, fail_settle="--fail-settle" in sys.argv)
    print(f"fake facilitator on {f.url}", flush=True)
    f.httpd.serve_forever()
