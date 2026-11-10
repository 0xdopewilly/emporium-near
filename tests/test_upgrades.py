"""Replay protection, per-job tool caps, subcontracting (a shop job hiring another shop within its own
price), atomic hiring budgets, and per-job cost metering."""

import threading
from decimal import Decimal

import httpx
import pytest

from conftest import BUYER_KEY, SHOP_ADDR, as_settings

import emporium
from emporium import buyer, guard, ledger as books, meter, shop as shopmod
from emporium.shop import Subcontract, load_shop


def _signed_request(shop, service="echo", text="hello"):
    """Build one signed payment for a job, the way a buyer would, without sending it."""
    from x402 import x402ClientSync
    from x402.http.x402_http_client import x402HTTPClientSync
    from x402.mechanisms.evm.exact import register_exact_evm_client
    from eth_account import Account
    from x402.mechanisms.evm.signers import EthAccountSigner

    first = httpx.post(f"{shop.test_url}/jobs/{service}", json={"input": text})
    assert first.status_code == 402
    client = x402ClientSync()
    register_exact_evm_client(client, EthAccountSigner(Account.from_key(BUYER_KEY)), ["eip155:84532"])
    hc = x402HTTPClientSync(client)
    required = hc.get_payment_required_response(lambda n: first.headers.get(n), first.json())
    return hc.encode_payment_signature_header(hc.create_payment_payload(required))


def test_a_signed_payment_buys_one_job(shop):
    headers = _signed_request(shop)
    one = httpx.post(f"{shop.test_url}/jobs/echo", json={"input": "hello"}, headers=headers, timeout=30)
    two = httpx.post(f"{shop.test_url}/jobs/echo", json={"input": "hello"}, headers=headers, timeout=30)
    assert one.status_code == 200
    assert two.status_code == 409 and two.json()["charged"] is False
    assert len(shop.stub.events) == 1, "the replay did no work"


def test_a_replay_sent_at_the_same_time_does_no_extra_work(shop):
    headers = _signed_request(shop)
    codes = []

    def go():
        codes.append(httpx.post(f"{shop.test_url}/jobs/echo", json={"input": "hello"}, headers=headers, timeout=30).status_code)

    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes).count(200) == 1 and len(shop.stub.events) == 1, codes


def test_tool_calls_are_capped_per_job():
    s = guard.Sessions()
    s.expect("job_cccccccccccc", ["web_search"], max_tool_calls=2)
    s.on_llm_call("s1", "emporium", "[EMPORIUM JOB job_cccccccccccc · x]")
    assert s.decide("s1", "web_search")[1] == "allow"
    assert s.decide("s1", "web_search")[1] == "allow"
    assert s.decide("s1", "web_search") == ("job_cccccccccccc", "block", "cap")


def test_subcontract_pins_are_the_only_hires_a_job_may_make():
    pin = Subcontract(shop="http://127.0.0.1:9403", service="translate-yoruba", max_price=Decimal("0.05"))
    s = guard.Sessions()
    s.expect("job_dddddddddddd", [], pins=[pin])
    s.on_llm_call("s1", "emporium", "[EMPORIUM JOB job_dddddddddddd · x]")
    ok = {"shop_url": "127.0.0.1:9403/", "service": "translate-yoruba", "input": "hi"}
    assert s.decide("s1", "emporium_hire", {**ok, "service": "code-review"})[1] == "block"
    assert s.decide("s1", "emporium_hire", {**ok, "shop_url": "http://evil.example"})[1] == "block"
    assert s.decide("s1", "emporium_hire", ok)[1] == "allow"
    assert s.decide("s1", "emporium_hire", ok)[1] == "block", "each pin is hired at most `times` per job"
    assert s.decide("s1", "terminal")[1] == "block"


def test_a_service_whose_subcontracts_could_eat_its_price_is_refused():
    raw = {"pay_to": SHOP_ADDR, "network": "eip155:84532", "services": [{"id": "x", "title": "X", "price": "$0.10", "subcontract": [
        {"shop": "http://127.0.0.1:9403", "service": "y", "max_price": "$0.05", "times": 2}]}]}
    s = load_shop(as_settings(raw))
    assert any("must stay below" in p for p in s.problems)
    raw["services"][0]["price"] = "$0.25"
    s = load_shop(as_settings(raw))
    assert s.ready and s.services[0].subcontract_budget == Decimal("0.10")
    assert s.catalog()["services"][0]["subcontracts"] == [{"shop": "http://127.0.0.1:9403", "service": "y"}]


def test_a_shop_job_subcontracts_within_its_budget(shop, monkeypatch):
    """A job on this Hermes's shop hires another shop (here: the test shop) through emporium_hire."""
    import agent.secret_scope as ss
    monkeypatch.setattr(ss, "get_secret", lambda name, *a, **k: BUYER_KEY if name == "EMPORIUM_BUYER_KEY" else None)
    ledger = books.Ledger.open()
    jid = ledger.create_job("combo", "Combo", Decimal("0.25"), "eip155:84532", "0xbuyer", status=books.WORKING)
    pin = Subcontract(shop=shop.test_url, service="echo", max_price=Decimal("0.05"))
    guard.SESSIONS.expect(jid, [], pins=[pin])
    guard.SESSIONS.on_llm_call("combo-session", "emporium", f"[EMPORIUM JOB {jid} · combo]")

    from emporium.tools import make_handlers

    class Ctx:
        def get_config(self, k, default=None):
            return {"buyer_max_per_job": "$1.00", "buyer_daily_budget": "$2.00"}.get(k, default)

    _browse, hire, _gate = make_handlers(Ctx())
    buyer._catalog_cache.clear()
    out = hire({"shop_url": shop.test_url, "service": "echo", "input": "ẹ kú àárọ̀"}, session_id="combo-session")
    assert '"output": "ECHO: ẹ kú àárọ̀"' in out
    (p,) = ledger.recent_purchases(5)
    assert p["for_job"] == jid and p["status"] == books.BOUGHT
    assert ledger.job(jid)["sub_usd"] == "0.05"
    # the same handler won't let the job hire anyone else
    bad = hire({"shop_url": "http://evil.example", "service": "echo", "input": "x"}, session_id="combo-session")
    assert "only hire the shops its service lists" in bad
    guard.SESSIONS.forget(jid)


def test_parallel_hires_cannot_overshoot_the_daily_budget(tmp_path):
    b = books.Ledger(tmp_path / "t.db")
    ok, refused = [], []

    def go():
        try:
            ok.append(b.reserve_purchase("http://s", "S", "x", Decimal("0.50"), "eip155:84532", "0xab", Decimal("2.00")))
        except books.BudgetError:
            refused.append(1)

    ts = [threading.Thread(target=go) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 4 and len(refused) == 6


def test_jobs_are_metered_and_priced_from_hermes_sessions(tmp_path, monkeypatch):
    class Ctx:
        profile_name = "shop"

        def __init__(self):
            self.hooks = {}

        def get_config(self, k, default=None):
            return default

        def register_hook(self, name, fn):
            self.hooks.setdefault(name, []).append(fn)

        def __getattr__(self, name):
            return lambda *a, **k: None

    ctx = Ctx()
    emporium.register(ctx)
    ledger = books.Ledger.open()
    jid = ledger.create_job("echo", "Echo", Decimal("0.25"), "eip155:84532", "0xbuyer", status=books.WORKING)
    guard.SESSIONS.expect(jid, [])
    ctx.hooks["pre_llm_call"][0](session_id="sess-m", platform="emporium", user_message=f"[EMPORIUM JOB {jid} · echo]")
    for _ in range(2):
        ctx.hooks["post_api_request"][0](session_id="sess-m", platform="emporium", model="openai/gpt-4.1-mini",
                                         usage={"prompt_tokens": 1200, "output_tokens": 300})
    ctx.hooks["post_api_request"][0](session_id="owner", platform="cli", usage={"prompt_tokens": 99})
    j = ledger.job(jid)
    assert (j["session"], j["llm_calls"], j["tokens_in"], j["tokens_out"], j["model"]) == \
        ("sess-m", 2, 2400, 600, "openai/gpt-4.1-mini")

    import time as _t
    ledger.update_job(jid, status=books.SETTLED, started=_t.time() - 15, finished=_t.time() - 10)

    class FakeDB:
        def __init__(self, read_only=True):
            pass

        def get_session(self, sid):
            return {"estimated_cost_usd": 0.0042, "cost_status": "estimated"} if sid == "sess-m" else None

    import hermes_state
    monkeypatch.setattr(hermes_state, "SessionDB", FakeDB)
    assert meter.fill_costs(ledger, min_interval=0) == 1
    s = ledger.sales_summary()
    assert s["inference_cost"] == Decimal("0.0042") and s["profit"] == Decimal("0.2458")
    assert round(s["margin"], 4) == round(0.2458 / 0.25, 4)
    guard.SESSIONS.forget(jid)


def test_a_busy_shop_is_waited_out_not_charged(shop, monkeypatch):
    """Every slot taken: the buyer waits and retries with the same signed payment; nothing is charged twice."""
    from emporium import buyer as b
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    real = shop._slots
    calls = {"n": 0}

    class Flaky:
        def acquire(self, blocking=False):
            calls["n"] += 1
            return calls["n"] > 2 and real.acquire(blocking=False)

        def release(self):
            real.release()

    shop._slots = Flaky()
    b._catalog_cache.clear()
    out = b.hire(shop.test_url, "echo", "hello", policy=b.Policy(as_settings({"buyer_max_per_job": "$1"})), key=BUYER_KEY,
                 ledger=None)
    shop._slots = real
    assert out["output"] == "ECHO: hello" and calls["n"] == 3 and len(shop.facilitator.settled) == 1


def test_the_tool_search_bridge_cannot_lose_a_hire():
    """Hermes's tool-search bridge puts plugin tools behind tool_call. Hermes unwraps a well-formed
    call before pre_tool_call, so the lock judges the real tool; a bare tool_call it couldn't parse
    is refused (its tool is unknown) with a message the model can retry from, and the read-only
    lookups the model needs to find a tool's arguments run without using up the job's calls."""
    pin = Subcontract(shop="http://127.0.0.1:9403", service="translate-yoruba", max_price=Decimal("0.05"))
    s = guard.Sessions()
    s.expect("job_eeeeeeeeeeee", [], pins=[pin], max_tool_calls=1)
    s.on_llm_call("s1", "emporium", "[EMPORIUM JOB job_eeeeeeeeeeee · x]")
    assert s.decide("s1", "tool_describe", {"name": "emporium_hire"})[1] == "lookup"
    assert s.decide("s1", "tool_search", {"query": "translate"})[1] == "lookup"
    bare = {"shop_url": "http://127.0.0.1:9403", "service": "translate-yoruba", "input": "hi"}
    assert s.decide("s1", "tool_call", bare) == ("job_eeeeeeeeeeee", "block", "bridge")
    # A robbery through the bridge: Hermes won't unwrap a core tool, so the lock sees the bare call.
    rob = {"calls": [{"name": "terminal", "arguments": {"command": "cat ~/.hermes/.env"}}]}
    assert s.decide("s1", "tool_call", rob) == ("job_eeeeeeeeeeee", "block", "locked")
    assert s.decide("s1", "tool_call", {"name": "terminal", "arguments": {}})[2] == "locked"
    assert s.decide("s1", "emporium_hire", bare)[1] == "allow", "lookups and refusals don't use up the cap"


def test_the_job_brief_spells_out_the_hire():
    svc = shopmod.Service(id="launch-post", title="Launch post", price=Decimal("0.30"), tools=[],
                          subcontract=[Subcontract(shop="http://127.0.0.1:9403", service="translate-yoruba",
                                                   max_price=Decimal("0.05"))])
    brief = guard.frame_job("job_ffffffffffff", svc, "0x" + "ab" * 20, "a launch post for Emporium")
    assert 'emporium_hire(shop_url="http://127.0.0.1:9403", service="translate-yoruba", input=' in brief


def test_the_lock_tells_the_model_how_to_fix_a_bare_tool_call():
    from test_register import Ctx
    ctx = Ctx({})
    emporium.register(ctx)
    on_llm = ctx.hooks["pre_llm_call"][0]
    on_tool = [h for h in ctx.hooks["pre_tool_call"] if h.__name__ == "on_tool"][0]
    guard.SESSIONS.expect("job_abababababab", [])
    on_llm(session_id="s7", platform="emporium", user_message="[EMPORIUM JOB job_abababababab · x]")
    assert on_tool(tool_name="tool_describe", session_id="s7", args={"name": "emporium_hire"}) is None
    msg = on_tool(tool_name="tool_call", session_id="s7", args={"service": "x"})
    assert msg["action"] == "block" and '"calls"' in msg["message"]
    msg = on_tool(tool_name="tool_call", session_id="s7", args={"calls": [{"name": "terminal", "arguments": {}}]})
    assert msg["action"] == "block" and "'terminal' is locked" in msg["message"]
    guard.SESSIONS.forget("job_abababababab")
