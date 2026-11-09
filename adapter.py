"""The shop's front door: a gateway platform whose "messages" are paid jobs.

    buyer ──POST /jobs/<service>──▶ 402 + price (x402)
    buyer ──POST + signed USDC authorization──▶ verify with the facilitator (nothing moves yet)
          ──▶ the job goes into its own fresh gateway session, fenced and tool-locked (guard.py)
          ◀── the agent's reply (send() with notify)
          ──▶ settle on-chain ──▶ 200 + the deliverable + the receipt (tx hash)

The buyer is charged only when the work is delivered: a failed or timed-out job is never settled,
and a job whose settlement fails gets no deliverable. Transport is stdlib ``http.server`` on a
daemon thread (the same shape as Hermes's bundled A2A adapter); x402 runs in its sync flavour on
that thread, and only the hand-off to the agent crosses into the gateway's event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import re
import select
import socket
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome

from . import guard, ledger as books, meter, storefront
from .shop import Shop, load_shop

logger = logging.getLogger(__name__)

_MAX_BODY = 256 * 1024
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")
_EXPOSE = "PAYMENT-REQUIRED, PAYMENT-RESPONSE, X-PAYMENT-RESPONSE"
# Settlement failures that a retry can't fix: the authorization itself is bad, spent or unfunded.
_SETTLE_FINAL = ("insufficient", "signature", "expired", "valid_before", "valid_after", "nonce", "used",
                 "recipient", "amount", "scheme", "network_mismatch")
SETTLE_ATTEMPTS = 5
SETTLE_MARGIN = 45   # seconds an authorization must outlive the job's timeout, so it can still settle
MAX_CONNECTIONS = 64


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else (addr or "anonymous")


def nonce_of(payload: Any) -> str:
    """The authorization's one-time nonce (EIP-3009 or Permit2): one signed payment buys one job."""
    try:
        body = getattr(payload, "payload", None) or {}
        for key in ("authorization", "permit2Authorization"):
            nonce = (body.get(key) or {}).get("nonce")
            if nonce not in (None, ""):
                return str(nonce)
    except Exception:
        pass
    return ""


def valid_before_of(payload: Any) -> Optional[int]:
    """When the buyer's signed authorization expires (EIP-3009 validBefore or Permit2 deadline)."""
    try:
        body = getattr(payload, "payload", None) or {}
        auth = body.get("authorization") or {}
        if auth.get("validBefore") not in (None, ""):
            return int(auth["validBefore"])
        p2 = body.get("permit2Authorization") or {}
        if p2.get("deadline") not in (None, ""):
            return int(p2["deadline"])
    except Exception:
        pass
    return None


def client_gone(handler: BaseHTTPRequestHandler) -> bool:
    """True when the buyer has hung up (the socket reads EOF). Settling then would charge someone
    who can no longer receive the result."""
    try:
        sock = handler.connection
        ready, _, _ = select.select([sock], [], [], 0)
        if not ready:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except Exception:
        return True


def payer_of(payload: Any) -> str:
    """The paying wallet, from an exact-scheme payload (EIP-3009 or Permit2 authorization)."""
    try:
        body = getattr(payload, "payload", None) or {}
        for key in ("authorization", "permit2Authorization"):
            frm = (body.get(key) or {}).get("from")
            if isinstance(frm, str) and _ADDR.match(frm):
                return frm
    except Exception:
        pass
    return ""


class _Req:
    """x402's HTTPAdapter over a stdlib request."""

    def __init__(self, handler: BaseHTTPRequestHandler, path: str, base_url: str):
        self._h, self._path, self._base = handler, path, base_url

    def get_header(self, name: str) -> Optional[str]:
        return self._h.headers.get(name)

    def get_method(self) -> str:
        return self._h.command

    def get_path(self) -> str:
        return self._path

    def get_url(self) -> str:
        return self._base + self._path

    def get_accept_header(self) -> str:
        return self._h.headers.get("Accept", "")

    def get_user_agent(self) -> str:
        return self._h.headers.get("User-Agent", "")

    def get_query_params(self) -> dict:
        return {}

    def get_query_param(self, name: str) -> Optional[str]:
        return None

    def get_body(self) -> Any:
        return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "HermesEmporium/0.2"
    protocol_version = "HTTP/1.1"
    timeout = 30  # a client that stalls mid-request is dropped instead of holding a thread

    @property
    def shop_adapter(self) -> "EmporiumAdapter":
        return self.server.adapter  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # quiet: the ledger is the log
        logger.debug("emporium http: " + fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str, headers: Optional[dict] = None) -> None:
        # One request per connection: an early refusal may leave an unread body on the socket, and a
        # pooling proxy must never see it parsed as the next buyer's request.
        self.close_connection = True
        self.send_response(code)
        self.send_header("Connection", "close")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", _EXPOSE)
        for k, v in (headers or {}).items():
            if k.lower() not in ("content-type", "content-length", "access-control-allow-origin",
                                 "access-control-expose-headers"):
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, code: int, payload: Any, headers: Optional[dict] = None) -> None:
        self._send(code, json.dumps(payload, indent=1, default=str).encode(), "application/json", headers)

    def do_OPTIONS(self):
        self.close_connection = True
        self.send_response(204)
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, PAYMENT-SIGNATURE, X-PAYMENT")
        self.send_header("Access-Control-Expose-Headers", _EXPOSE)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        a = self.shop_adapter
        try:
            if path == "/":
                self._send(200, storefront.render(a.shop, a.books).encode(), "text/html; charset=utf-8")
            elif path in ("/.well-known/emporium.json", "/catalog"):
                cat = a.shop.catalog()
                cat["open"] = cat["open"] and a.books.is_open()
                self.json(200, cat)
            elif path == "/feed":
                meter.fill_costs(a.books)
                self.json(200, storefront.feed(a.shop, a.books))
            elif path == "/healthz":
                self.json(200, {"ok": True, "open": a.books.is_open(), "services": len(a.shop.services)})
            else:
                self.json(404, {"error": "not found"})
        except Exception:
            logger.exception("emporium: GET %s failed", path)
            self.json(500, {"error": "internal error"})

    do_HEAD = do_GET

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if not path.startswith("/jobs/"):
            return self.json(404, {"error": "not found"})
        try:
            self.shop_adapter.handle_job(self, path[len("/jobs/"):])
        except Exception:
            logger.exception("emporium: job request failed")
            with contextlib.suppress(Exception):
                self.json(500, {"error": "internal error", "charged": False})


class _Server(ThreadingHTTPServer):
    """A ThreadingHTTPServer that refuses connections beyond MAX_CONNECTIONS instead of spawning
    unbounded threads (each stalled client holds one until its 30 s timeout)."""

    daemon_threads = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._conns = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self._conns.acquire(blocking=False):
            with contextlib.suppress(Exception):
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._conns.release()


class EmporiumAdapter(BasePlatformAdapter):
    """Inbound platform: paid jobs in, deliverables out."""

    def __init__(self, config: Any, settings: Callable[[str, Any], Any], profile: str = ""):
        super().__init__(config=config, platform=Platform(guard.PLATFORM))
        self._settings = settings
        self._profile = profile
        self.shop: Shop = load_shop(settings, profile)
        self.books: books.Ledger = None  # type: ignore[assignment]  # opened in connect()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._x402: Any = None
        self._slots = threading.BoundedSemaphore(self.shop.max_concurrent_jobs)
        self._pending: dict[str, tuple[str, Future]] = {}  # chat_id -> (job_id, future)
        self._services: dict[str, Any] = {}                # job_id -> the service it bought (for toolsets)
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "Emporium"

    @property
    def authorization_is_upstream(self) -> bool:
        """Every job is authorized by its payment (verified with the x402 facilitator before the
        agent sees it); there is no human sender to allowlist or pair."""
        return True

    # ---- lifecycle -------------------------------------------------------------------------
    def _build_x402(self, shop: Shop) -> Any:
        from x402 import x402ResourceServerSync
        from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync
        from x402.http.types import PaymentOption, RouteConfig
        from x402.http.x402_http_server import x402HTTPResourceServerSync
        from x402.mechanisms.evm.exact import register_exact_evm_server

        server = x402ResourceServerSync(HTTPFacilitatorClientSync(FacilitatorConfig(url=shop.facilitator_url)))
        register_exact_evm_server(server, shop.network)
        routes = {
            f"POST /jobs/{s.id}": RouteConfig(
                accepts=PaymentOption(scheme="exact", pay_to=shop.pay_to, price=s.price_label, network=shop.network,
                                      max_timeout_seconds=s.timeout_seconds + 120),
                description=f"{shop.name}: {s.title}", mime_type="application/json", service_name=shop.name)
            for s in shop.services
        }
        http = x402HTTPResourceServerSync(server, routes)
        http.initialize()  # asks the facilitator what it supports; fails fast on a bad network
        return http

    async def connect(self, **_kwargs) -> bool:
        self._loop = asyncio.get_running_loop()
        self.shop = load_shop(self._settings, self._profile)
        if not self.shop.ready:
            msg = "shop not ready: " + "; ".join(self.shop.problems)
            logger.error("Emporium: %s", msg)
            self._set_fatal_error("not_configured", msg, retryable=False)
            return False
        # Paid jobs run on this profile: its memory, persona and context files would reach every buyer's
        # prompt, and its memory machinery would record buyers' turns. Don't open until it's sealed.
        from .shop import SEAL_HINT, UNSEALED_SETTING, profile_risks
        risks = await asyncio.to_thread(profile_risks, self.shop.profile)
        if risks and not self.shop.allow_unsealed:
            msg = (f"shop not opened: {'; '.join(risks)}. To seal it, {SEAL_HINT}. "
                   f"(To open on this profile anyway, set {UNSEALED_SETTING}: true.)")
            logger.error("Emporium: %s", msg)
            self._set_fatal_error("profile_not_sealed", msg, retryable=False)
            return False
        try:
            self.books = await asyncio.to_thread(books.Ledger.open)
            stale = await asyncio.to_thread(self.books.fail_stale_jobs)
            if stale:
                logger.warning("Emporium: %d job(s) were cut off by a restart; none of them was charged", stale)
            self._x402 = await asyncio.to_thread(self._build_x402, self.shop)
        except Exception as e:
            logger.error("Emporium: x402 setup failed: %s", e)
            self._set_fatal_error("x402_setup", f"x402 setup failed: {e}", retryable=True)
            return False
        self._slots = threading.BoundedSemaphore(self.shop.max_concurrent_jobs)
        try:
            self._httpd = _Server((self.shop.host, self.shop.port), _Handler)
        except OSError as e:
            logger.error("Emporium: could not bind %s:%s: %s", self.shop.host, self.shop.port, e)
            self._set_fatal_error("bind_failed", f"bind failed: {e}", retryable=True)
            return False
        self._httpd.daemon_threads = True
        self._httpd.adapter = self  # type: ignore[attr-defined]
        self.shop.port = self._httpd.server_address[1]  # the real port when 0 asked for any
        threading.Thread(target=self._httpd.serve_forever, name="emporium-http", daemon=True).start()
        self._mark_connected(listener_base=f"http://{self.shop.host}:{self.shop.port}")
        for risk in risks:
            logger.warning("Emporium: open on an unsealed profile (%s is on): %s. To seal it, %s",
                           UNSEALED_SETTING, risk, SEAL_HINT)
        logger.info("Emporium: %r open at %s · %d service(s) · paid to %s on %s",
                    self.shop.name, self.shop.base_url(), len(self.shop.services), self.shop.pay_to, self.shop.network_label)
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._httpd is not None:
            with contextlib.suppress(Exception):
                self._httpd.shutdown()
                self._httpd.server_close()
            self._httpd = None
        with self._lock:
            for _chat, (_job, fut) in list(self._pending.items()):
                if not fut.done():
                    fut.set_result((False, "shop shutting down"))
            self._pending.clear()

    # ---- the paid job ----------------------------------------------------------------------
    def handle_job(self, h: _Handler, service_id: str) -> None:
        from x402.http.types import HTTPRequestContext, HTTPTransportContext

        shop, svc = self.shop, self.shop.service(service_id)
        if svc is None:
            return h.json(404, {"error": f"no service {service_id!r}", "services": [s.id for s in shop.services]})
        if not (shop.open and self.books.is_open()):
            return h.json(503, {"error": "the shop is closed right now", "charged": False})
        try:
            length = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY:
            return h.json(400, {"error": "send a JSON body like {\"input\": \"...\"}", "charged": False})
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
        except ValueError:
            return h.json(400, {"error": "body is not valid JSON", "charged": False})
        buyer_input = body.get("input") if isinstance(body, dict) else None
        reason = guard.screen_input(buyer_input, svc)
        if reason:
            return h.json(400, {"error": reason, "charged": False})

        path = f"/jobs/{svc.id}"
        req = _Req(h, path, shop.base_url())
        ctx = HTTPRequestContext(adapter=req, path=path, method="POST",
                                 payment_header=h.headers.get("PAYMENT-SIGNATURE") or h.headers.get("X-PAYMENT"))
        result = self._x402.process_http_request(ctx)
        if result.type == "payment-error":
            resp = result.response
            if resp is None:
                return h.json(402, {"error": "payment required"})
            return h.json(resp.status, resp.body or {"error": "payment required", "price": svc.price_label}, resp.headers)
        if result.type != "payment-verified":
            return h.json(500, {"error": "unexpected payment state", "charged": False})

        expires = valid_before_of(result.payment_payload)
        if expires is not None and expires < time.time() + svc.timeout_seconds + SETTLE_MARGIN:
            return h.json(402, {"error": f"the payment authorization expires too soon; it must stay valid for at least "
                                         f"{svc.timeout_seconds + SETTLE_MARGIN}s so the job can finish and settle",
                                "charged": False})
        payer = payer_of(result.payment_payload)
        if payer and self.books.payer_jobs_since(payer, time.time() - 3600) >= shop.jobs_per_payer_per_hour:
            return h.json(429, {"error": "too many jobs from this wallet in the last hour", "charged": False})
        if not self._slots.acquire(blocking=False):
            return h.json(503, {"error": "all hands busy, try again in a minute", "charged": False})
        jid = books.new_id("job")
        if not self.books.claim_nonce(nonce_of(result.payment_payload), jid):
            self._slots.release()
            return h.json(409, {"error": "this payment authorization was already used for a job; sign a new one",
                                "charged": False})
        jid = self.books.create_job(svc.id, svc.title, svc.price, shop.network, payer, status=books.WORKING,
                                    input_chars=len(buyer_input), job_id=jid)
        chat_id = guard.CHAT_PREFIX + jid
        try:
            self.books.update_job(jid, started=time.time())
            ok, text = self._run_job(jid, chat_id, svc, payer, buyer_input)
            if not ok or not text.strip():
                self.books.update_job(jid, status=books.FAILED, error=(text or "empty reply")[:300], finished=time.time())
                code = 504 if text == "timeout" else 502
                return h.json(code, {"job": jid, "error": f"the job did not complete ({text or 'empty reply'}); you were not charged",
                                     "charged": False})
            output = guard.strip_private(guard.scrub_output(text), svc.instructions)
            digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
            if client_gone(h):
                self.books.update_job(jid, status=books.FAILED, error="the buyer disconnected before delivery",
                                      output_chars=len(output), finished=time.time())
                return None
            settle = self._settle(result, ctx, jid, payer, svc.price)
            if not settle.success:
                self.books.update_job(jid, status=books.UNPAID, error=str(settle.error_reason or "settle failed")[:300],
                                      output_chars=len(output), finished=time.time())
                resp = settle.response
                return h.json(resp.status if resp else 402,
                              {"job": jid, "error": f"payment did not settle ({settle.error_reason}); the result is withheld",
                               "charged": False}, resp.headers if resp else None)
            tx = settle.transaction if _TX.match(str(settle.transaction or "")) else ""
            self.books.update_job(jid, status=books.SETTLED, tx=tx, output_chars=len(output), finished=time.time(),
                                  payer=(settle.payer or payer or ""), output_sha256=digest)
            return h.json(200, {
                "job": jid, "service": svc.id, "title": svc.title, "output": output,
                "receipt": {"amount": svc.price_label, "asset": "USDC", "network": shop.network,
                            "network_label": shop.network_label, "payer": settle.payer or payer, "pay_to": shop.pay_to,
                            "tx": tx, "explorer": shop.explorer(tx), "shop": shop.name, "output_sha256": digest},
            }, settle.headers)
        finally:
            self._slots.release()
            guard.SESSIONS.forget(jid)
            with self._lock:
                self._pending.pop(chat_id, None)
                self._services.pop(jid, None)
            # The job's inference cost reaches Hermes's session store a moment after the turn ends.
            t = threading.Timer(6.0, lambda: meter.fill_costs(self.books, min_interval=0))
            t.daemon = True
            t.start()

    def _settle(self, result: Any, ctx: Any, jid: str, payer: str = "", price: Any = None) -> Any:
        """Settle a verified payment, retrying a transient facilitator failure (a public facilitator
        occasionally fails to land the transfer). The EIP-3009 authorization carries a one-time
        nonce, so a retry can never charge twice. If a retry reports the nonce as already used, an
        earlier attempt may have landed after all: the chain decides, not the error message."""
        from x402.http.types import HTTPTransportContext
        seen_tx = ""
        for attempt in range(1, SETTLE_ATTEMPTS + 1):
            settle = self._x402.process_settlement(
                result.payment_payload, result.payment_requirements, context=ctx,
                declared_extensions=result.declared_extensions, transport_context=HTTPTransportContext(request=ctx),
                before_handler_settlement=result.before_handler_settlement)
            if settle.success:
                return settle
            if _TX.match(str(getattr(settle, "transaction", "") or "")):
                seen_tx = settle.transaction
            reason = str(settle.error_reason or "").lower()
            if (attempt > 1 and "nonce" in reason) or seen_tx:
                landed = self._landed_on_chain(result.payment_payload, payer, seen_tx, jid, price)
                if landed:
                    settle.success, settle.transaction, settle.error_reason = True, landed, None
                    return settle
            if attempt == SETTLE_ATTEMPTS or any(k in reason for k in _SETTLE_FINAL):
                if not seen_tx and payer:  # last word goes to the chain: did any attempt land after all?
                    landed = self._landed_on_chain(result.payment_payload, payer, "", jid, price)
                    if landed:
                        settle.success, settle.transaction, settle.error_reason = True, landed, None
                return settle
            logger.info("Emporium: settlement attempt %d for %s failed (%s), retrying", attempt, jid, reason[:120])
            time.sleep(2.0 * attempt + random.uniform(0, 1.5))  # spread retries out: a busy facilitator recovers
        return settle

    def _landed_on_chain(self, payload: Any, payer: str, seen_tx: str, jid: str, want: Any = None) -> str:
        """The tx hash if this job's payment reached the shop's wallet on-chain, else ''."""
        from . import chain
        shop = self.shop
        try:
            candidates = [seen_tx] if seen_tx else []
            if payer:
                found = chain.authorization_tx(shop.network, payer, nonce_of(payload), shop.rpc_url)
                if found:
                    candidates.append(found)
            for tx in candidates:
                got = chain.paid_to(shop.network, tx, shop.pay_to, shop.rpc_url)
                if got > 0 and (want is None or got >= want):
                    logger.warning("Emporium: settlement for %s reported a failure but landed on-chain in %s", jid, tx)
                    return tx
        except Exception:
            logger.info("Emporium: couldn't check %s's payment on-chain", jid, exc_info=True)
        return ""

    def _run_job(self, jid: str, chat_id: str, svc: Any, payer: str, buyer_input: str) -> tuple[bool, str]:
        """Hand the job to the agent in its own session and wait for the final reply."""
        if self._loop is None or self._message_handler is None:
            return False, "agent gateway not ready"
        fut: Future = Future()
        with self._lock:
            self._pending[chat_id] = (jid, fut)
            self._services[jid] = svc
        guard.SESSIONS.expect(jid, svc.tools, svc.subcontract, svc.max_tool_calls)
        event = MessageEvent(
            text=guard.frame_job(jid, svc, payer, buyer_input), message_type=MessageType.TEXT, message_id=jid,
            source=self.build_source(chat_id=chat_id, chat_name=f"emporium:{svc.id}", chat_type="dm",
                                     user_id=(payer or "anonymous").lower(), user_name=_short(payer)))
        try:
            dispatched = asyncio.run_coroutine_threadsafe(self.handle_message(event), self._loop)
        except Exception as e:
            return False, f"dispatch failed: {e}"

        def _dispatch_done(f: Future) -> None:
            if f.cancelled() or f.exception() is not None:
                self._resolve(chat_id, False, "the job could not be dispatched to the agent")
        dispatched.add_done_callback(_dispatch_done)
        try:
            return fut.result(timeout=svc.timeout_seconds)
        except FuturesTimeout:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._interrupt(chat_id), self._loop)
            return False, "timeout"

    async def _interrupt(self, chat_id: str) -> None:
        """Stop a timed-out job's agent turn so it stops spending (the buyer was not charged either way)."""
        keys = [k for k in list(getattr(self, "_active_sessions", {}) or {}) if str(k).endswith(":" + chat_id)]
        for key in keys or [""]:
            with contextlib.suppress(Exception):
                await self.interrupt_session_activity(key, chat_id)
        if not keys:
            logger.info("Emporium: no active session found to interrupt for %s", chat_id)

    def _resolve(self, chat_id: str, ok: bool, text: str) -> bool:
        with self._lock:
            entry = self._pending.get(chat_id)
        if entry is None or entry[1].done():
            return False
        entry[1].set_result((ok, text))
        return True

    # ---- gateway → shop --------------------------------------------------------------------
    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None):
        """The agent's final reply (``metadata['notify']``) completes the job; progress and
        interim sends are not deliverables and are dropped."""
        if (metadata or {}).get("notify"):
            jid = chat_id[len(guard.CHAT_PREFIX):] if chat_id.startswith(guard.CHAT_PREFIX) else ""
            rules = guard.SESSIONS.rules_for_job(jid) if jid else None
            if rules is not None and (rules.failed or rules.llm_ok == 0):
                # The gateway sends its apology ("Sorry, I encountered an error…") as the final reply when a
                # turn fails. That is not the service the buyer paid for, so it is never settled.
                self._resolve(chat_id, False, f"the agent hit an error ({rules.failed or 'no model response'})")
            else:
                self._resolve(chat_id, True, content or "")
        return SendResult(success=True, message_id=str(int(time.time() * 1000)))

    # Deliverables are text. Files, images and voice the gateway splits out of a reply are dropped
    # (the services sold never produce them), instead of turning into a "couldn't send" notice.
    async def send_image(self, chat_id: str, *a, **kw):
        return SendResult(success=True, message_id="")

    async def send_image_file(self, chat_id: str, *a, **kw):
        return SendResult(success=True, message_id="")

    async def send_document(self, chat_id: str, *a, **kw):
        return SendResult(success=True, message_id="")

    async def send_voice(self, chat_id: str, *a, **kw):
        return SendResult(success=True, message_id="")

    async def send_video(self, chat_id: str, *a, **kw):
        return SendResult(success=True, message_id="")

    def toolsets_for_source(self, source: Any) -> Optional[list[str]]:
        """Load only what the job's service sells. Hermes would otherwise give a shop session the
        platform's default toolsets (terminal, files, browser, memory…); the tool lock refuses those,
        but it is better that the model never sees them. ``no_mcp`` keeps MCP servers out too."""
        chat_id = str(getattr(source, "chat_id", "") or "")
        jid = chat_id[len(guard.CHAT_PREFIX):] if chat_id.startswith(guard.CHAT_PREFIX) else ""
        with self._lock:
            svc = self._services.get(jid)
        sets = ["no_mcp"]
        tools = set(getattr(svc, "tools", []) or [])
        if "web_extract" in tools:
            sets.append("web")
        elif "web_search" in tools:
            sets.append("search")
        logger.debug("Emporium: job %s loads toolsets %s", jid or chat_id, sets)
        return sets

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        chat_id = guard.CHAT_PREFIX + str(getattr(event, "message_id", "") or "")
        if outcome == ProcessingOutcome.FAILURE:
            self._resolve(chat_id, False, "agent processing failed")
        elif outcome == ProcessingOutcome.CANCELLED:
            self._resolve(chat_id, False, "cancelled")
        else:
            streamed = getattr(event, "_streamed_final_response", "")
            if isinstance(streamed, str) and streamed.strip():
                self._resolve(chat_id, True, streamed)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": f"emporium:{chat_id}", "type": "dm"}
