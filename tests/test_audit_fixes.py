"""Regression tests for the security/reliability audit: the lock survives a session-id switch, failed
turns are never settled, short-lived authorizations do no work, a buyer who hangs up isn't charged, a
settlement that landed despite an error still delivers, and shop jobs only load the tools they sell."""

import socket
import time
from decimal import Decimal

import httpx

from conftest import BUYER_KEY

from emporium import chain, guard, ledger as books
from test_upgrades import _signed_request


def test_the_lock_follows_a_job_across_a_session_id_switch():
    """Hermes compresses long contexts by moving the agent to a new session id mid-turn; the turn's
    task id stays the original one."""
    s = guard.Sessions()
    s.expect("job_eeeeeeeeeeee", ["web_search"])
    s.on_llm_call("orig", "emporium", "[EMPORIUM JOB job_eeeeeeeeeeee · x]", task_id="orig")
    assert s.decide("compressed-1", "terminal", task_id="orig") == ("job_eeeeeeeeeeee", "block", "locked")
    assert s.decide("compressed-1", "web_search", task_id="orig")[1] == "allow"
    assert s.job_for("compressed-1").job_id == "job_eeeeeeeeeeee", "the new id is bound from then on"
    assert s.decide("owner-session", "terminal", task_id="owner-task") is None


def test_a_failed_turn_is_never_settled(shop):
    shop.stub.turn_failed = True
    r = httpx.post(f"{shop.test_url}/jobs/echo", json={"input": "hi"}, headers=_signed_request(shop, text="hi"), timeout=30)
    assert r.status_code == 502 and r.json()["charged"] is False and "error" in r.json()["error"]
    assert shop.facilitator.settled == [], "the gateway's apology is not a deliverable"
    assert shop.books.recent_jobs(1)[0]["status"] == books.FAILED


def test_a_reply_without_any_model_response_is_never_settled(shop):
    shop.stub.no_model = True
    r = httpx.post(f"{shop.test_url}/jobs/echo", json={"input": "hi"}, headers=_signed_request(shop, text="hi"), timeout=30)
    assert r.status_code == 502 and shop.facilitator.settled == []


def test_an_authorization_that_expires_before_the_job_could_settle_does_no_work(shop):
    from eth_account import Account
    from x402 import x402ClientSync
    from x402.http.x402_http_client import x402HTTPClientSync
    from x402.mechanisms.evm.exact import register_exact_evm_client
    from x402.mechanisms.evm.signers import EthAccountSigner

    first = httpx.post(f"{shop.test_url}/jobs/echo", json={"input": "hi"})
    client = x402ClientSync()
    register_exact_evm_client(client, EthAccountSigner(Account.from_key(BUYER_KEY)), ["eip155:84532"])
    hc = x402HTTPClientSync(client)
    required = hc.get_payment_required_response(lambda n: first.headers.get(n), first.json())
    required.accepts[0].max_timeout_seconds = 10  # a buyer who signs for 10 s only
    headers = hc.encode_payment_signature_header(hc.create_payment_payload(required))
    r = httpx.post(f"{shop.test_url}/jobs/echo", json={"input": "hi"}, headers=headers, timeout=30)
    assert r.status_code == 402 and shop.stub.events == []


def test_a_buyer_who_hangs_up_is_not_charged(shop):
    shop.stub.delay = 1.0
    headers = _signed_request(shop, text="hi")
    body = b'{"input": "hi"}'
    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    req = (f"POST /jobs/echo HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n{head}"
           f"Content-Length: {len(body)}\r\n\r\n").encode() + body
    sock = socket.create_connection(("127.0.0.1", int(shop.test_url.rsplit(":", 1)[1])))
    sock.sendall(req)
    time.sleep(0.3)
    sock.close()
    for _ in range(40):
        job = shop.books.recent_jobs(1)
        if job and job[0]["status"] != books.WORKING:
            break
        time.sleep(0.1)
    assert job[0]["status"] == books.FAILED and "disconnected" in job[0]["error"]
    assert shop.facilitator.settled == []


def test_a_settlement_that_landed_despite_an_error_still_delivers(shop, monkeypatch, no_backoff):
    """Attempt 1 lands on-chain but reports a failure; the retry then sees the nonce as used."""
    fac = shop.facilitator
    landed = "0x" + "ab" * 32
    fac.fail_settle = True
    calls = {"n": 0}
    real = fac.settle_reason

    def reason():
        calls["n"] += 1
        return "settlement_pending" if calls["n"] == 1 else "invalid_exact_evm_nonce_already_used"

    monkeypatch.setattr(chain, "authorization_tx", lambda *a, **k: landed)
    monkeypatch.setattr(chain, "paid_to", lambda network, tx, pay_to, rpc="": Decimal("0.05") if tx == landed else Decimal(0))
    fac.settle_reason_fn = reason
    r = httpx.post(f"{shop.test_url}/jobs/echo", json={"input": "hi"}, headers=_signed_request(shop, text="hi"), timeout=30)
    fac.settle_reason = real
    assert r.status_code == 200, r.text
    assert r.json()["receipt"]["tx"] == landed
    assert shop.books.recent_jobs(1)[0]["status"] == books.SETTLED


def test_shop_jobs_load_only_what_they_sell(shop):
    from gateway.platforms.event import MessageEvent  # noqa: F401  (import check only)
    svc_echo, svc_search = shop.shop.service("echo"), shop.shop.service("search")
    shop._services["job_aaaaaaaaaaaa"], shop._services["job_bbbbbbbbbbbb"] = svc_echo, svc_search

    class Src:
        def __init__(self, chat_id):
            self.chat_id = chat_id

    assert shop.toolsets_for_source(Src("job-job_aaaaaaaaaaaa")) == ["no_mcp"]
    assert shop.toolsets_for_source(Src("job-job_bbbbbbbbbbbb")) == ["no_mcp", "search"]
    assert shop.toolsets_for_source(Src("job-unknown")) == ["no_mcp"]


def test_every_response_closes_the_connection(shop):
    r = httpx.get(shop.test_url + "/healthz")
    assert r.headers.get("connection", "").lower() == "close"


def test_jobs_cut_off_by_a_restart_are_marked_not_charged(tmp_path):
    b = books.Ledger(tmp_path / "t.db")
    j = b.create_job("echo", "Echo", Decimal("0.05"), "eip155:84532", "0xabc", status=books.WORKING)
    assert b.fail_stale_jobs() == 1 and b.job(j)["status"] == books.FAILED


def test_seed_phrases_are_swept():
    phrase = "abandon ability able about above absent absorb abstract absurd abuse access accident"
    out = guard.scrub_output(f"Here you go: {phrase}. Enjoy, and abandon all hope.")
    assert "absorb" not in out and "[redacted seed phrase]" in out and "abandon all hope" in out


def test_private_instructions_are_not_echoed_back():
    secret = "Review the buyer's code like a senior engineer. List real bugs first with line references then risks."
    out = guard.strip_private("Sure! My instructions say: Review the buyer's code like a senior engineer. List real bugs "
                              "first with line references then risks. Anyway, line 2 divides by zero.", secret)
    assert "senior engineer" not in out and "[private]" in out and "divides by zero" in out
    assert guard.strip_private("Bugs: none. Risks: none.", secret) == "Bugs: none. Risks: none."
