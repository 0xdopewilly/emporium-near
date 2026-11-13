"""The keyless demo (demo.py): POST /demo/hire runs one of the shop's own services over the normal x402
path, paid from the owner's demo account, against tests/fake_near.py's chain. Off unless the owner set
it up, testnet only, and the caller chooses a service and an input and nothing else."""

import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from conftest import as_settings, start_shop
from fake_near import FakeNear

from emporium import demo as demo_mod, ledger as books, near, near_x402
from emporium import shop as shop_mod

NET = "near:testnet"


def seeded(text: str) -> near.KeyPair:
    return near.KeyPair("ed25519:" + near.b58encode(hashlib.sha256(text.encode()).digest()))


# Throwaway keys, used only against the fake chain.
RELAYER, DEMO, BUYER = seeded("demo-test-relayer"), seeded("demo-test-demo-buyer"), seeded("demo-test-buyer")
SECRETS = {"EMPORIUM_NEAR_RELAYER_KEY": RELAYER.secret(), "EMPORIUM_DEMO_BUYER_KEY": DEMO.secret()}


@pytest.fixture
def chain(monkeypatch):
    c = FakeNear().start()
    c.add_account("relay.testnet", RELAYER, near_amount=1)
    c.add_account("shop.testnet")
    c.add_account("demo.testnet", DEMO, near_amount=0.03, usdc=2_000_000)
    c.add_account("buyer.testnet", BUYER, near_amount=0.03, usdc=2_000_000)
    monkeypatch.setitem(shop_mod.RPC_URLS, NET, c.url)
    monkeypatch.setitem(shop_mod.ARCHIVAL_RPC_URLS, NET, "")
    monkeypatch.setattr(near_x402.ExactNearFacilitator, "poll_seconds", 0.01)
    import agent.secret_scope as ss
    monkeypatch.setattr(ss, "get_secret", lambda name, *a, **k: SECRETS.get(name))
    yield c
    c.stop()


def settings(chain, **over):
    base = {
        "pay_to": "shop.testnet", "network": NET, "near_relayer_account": "relay.testnet", "rpc_url": chain.url,
        "shop_name": "Demo Test Emporium", "host": "127.0.0.1", "port": 0,
        "services": [
            {"id": "echo", "title": "Echo", "price": "$0.05", "instructions": "Repeat the input.", "tools": []},
            {"id": "pricey", "title": "Pricey", "price": "$3.00", "instructions": "x", "tools": []},
        ],
        "demo_buyer_account": "demo.testnet",
    }
    base.update(over)
    return base


@pytest.fixture
def demo_shop(chain):
    adapter, stop = start_shop(settings(chain))
    adapter.chain = chain
    yield adapter
    stop()


def post(shop, body, headers=None, raw=None):
    return httpx.post(shop.test_url + "/demo/hire", json=None if raw is not None else body, content=raw,
                      headers=headers, timeout=60)


def catalog(shop):
    return httpx.get(shop.test_url + "/.well-known/emporium.json").json()


def test_the_demo_is_off_unless_the_owner_sets_it_up(chain):
    adapter, stop = start_shop(settings(chain, demo_buyer_account=""))
    try:
        assert "demo" not in catalog(adapter)
        r = post(adapter, {"service": "echo", "input": "hi"})
        assert r.status_code == 403 and "no keyless demo" in r.json()["error"]
        assert not adapter.stub.events and chain.included == []
    finally:
        stop()


def test_a_keyless_hire_runs_the_shops_own_x402_path_and_returns_the_receipt(demo_shop):
    net = demo_shop.chain
    ad = catalog(demo_shop)["demo"]
    assert ad["enabled"] is True and ad["endpoint"] == demo_shop.test_url + "/demo/hire"
    assert (ad["daily_left"], ad["daily_jobs"], ad["payer"], ad["max_price"]) == (30, 30, "demo.testnet", "$0.50")
    demo_near = net.near_balance("demo.testnet")

    r = post(demo_shop, {"service": "echo", "input": "ping 42"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["output"] == "ECHO: ping 42" and out["service"] == "echo"
    rc = out["receipt"]
    assert (rc["payer"], rc["pay_to"], rc["settled_by"], rc["paid"], rc["network"]) == \
        ("demo.testnet", "shop.testnet", "relay.testnet", "$0.05", NET)
    assert near.TX_HASH.match(rc["tx"]) and rc["explorer"] == "https://testnet.nearblocks.io/txns/" + rc["tx"]
    assert rc["output_sha256"] == hashlib.sha256(out["output"].encode()).hexdigest()
    assert isinstance(rc["seconds"], float) and out["demo"] == {"payer": "demo.testnet", "daily_left": 29}
    # the chain: the demo account paid the listed price to the shop, and no gas
    assert net.usdc("shop.testnet") == 50_000 and net.usdc("demo.testnet") == 1_950_000
    assert net.near_balance("demo.testnet") == demo_near and len(net.included) == 1
    assert net.included[0]["signer_id"] == "relay.testnet"
    # the shop's books: one settled job, paid by the demo account, like any buyer's
    job = demo_shop.books.recent_jobs(1)[0]
    assert (job["status"], job["payer"], job["tx"]) == (books.SETTLED, "demo.testnet", rc["tx"])
    assert catalog(demo_shop)["demo"]["daily_left"] == 29
    # and nothing about the caller but a hash of its address is kept
    row = demo_shop.books._rows("SELECT * FROM demo_hires")[0]
    assert row["status"] == "delivered" and row["tx"] == rc["tx"] and "127.0.0.1" not in json.dumps(row)


def test_the_caller_chooses_a_service_and_input_and_nothing_else(demo_shop):
    net = demo_shop.chain
    for extra in ({"pay_to": "evil.testnet"}, {"amount": "1"}, {"network": "near:mainnet"},
                  {"shop": "http://evil.example"}, {"url": "http://evil.example"}, {"max_price": "$100"}):
        r = post(demo_shop, {"service": "echo", "input": "hi", **extra})
        assert r.status_code == 400 and "takes nothing else" in r.json()["error"], extra
    assert post(demo_shop, None, raw=b'["echo", "hi"]', headers={"Content-Type": "application/json"}).status_code == 400
    assert post(demo_shop, None, raw=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert not demo_shop.stub.events and net.included == []
    assert demo_shop.books.demo_used_today() == 0, "a refused request uses no demo job"


def test_only_this_shops_own_services_up_to_the_demo_price(demo_shop):
    r = post(demo_shop, {"service": "translate-yoruba", "input": "hi"})
    assert r.status_code == 404 and r.json()["services"] == ["echo", "pricey"]
    for sid in ("../jobs/echo", "http://other.example/jobs/echo", 7, None):
        assert post(demo_shop, {"service": sid, "input": "hi"}).status_code == 404, sid
    r = post(demo_shop, {"service": "pricey", "input": "hi"})
    assert r.status_code == 403 and "above the demo's $0.50 limit" in r.json()["error"]
    assert not demo_shop.stub.events and demo_shop.chain.included == []


def test_the_input_is_capped(demo_shop):
    assert post(demo_shop, {"service": "echo", "input": "x" * 2001}).status_code == 400
    assert "up to 2000" in post(demo_shop, {"service": "echo", "input": "x" * 2001}).json()["error"]
    assert post(demo_shop, {"service": "echo", "input": "   "}).status_code == 400
    assert post(demo_shop, {"service": "echo"}).status_code == 400
    assert not demo_shop.stub.events


def test_each_address_gets_a_few_jobs_every_10_minutes(chain):
    adapter, stop = start_shop(settings(chain, demo_jobs_per_address=2))
    try:
        me = {"X-Real-IP": "203.0.113.7"}  # the shop sits behind a proxy here (a loopback peer), as on Railway
        assert [post(adapter, {"service": "echo", "input": f"job {i}"}, me).status_code for i in range(2)] == [200, 200]
        r = post(adapter, {"service": "echo", "input": "job 3"}, me)
        assert r.status_code == 429 and "2 demo jobs per 10 minutes" in r.json()["error"]
        assert 0 < int(r.headers["Retry-After"]) <= 600
        # another address isn't held up by this one
        assert post(adapter, {"service": "echo", "input": "job 4"}, {"X-Real-IP": "198.51.100.9"}).status_code == 200
        assert len(chain.included) == 3
    finally:
        stop()


def test_the_daily_cap_holds_across_a_restart(chain, monkeypatch):
    adapter, stop = start_shop(settings(chain, demo_daily_jobs=2))
    try:
        for i in range(2):
            assert post(adapter, {"service": "echo", "input": f"day {i}"}, {"X-Real-IP": f"203.0.113.{i}"}).status_code == 200
        r = post(adapter, {"service": "echo", "input": "one more"}, {"X-Real-IP": "203.0.113.99"})
        assert r.status_code == 429 and "jobs for today are used up" in r.json()["error"] and "00:00 UTC" in r.json()["error"]
        assert catalog(adapter)["demo"]["daily_left"] == 0
    finally:
        stop()
    # a restart opens the ledger afresh: the count is read back from it, not from memory
    monkeypatch.setattr(books, "_SHARED", {})
    adapter, stop = start_shop(settings(chain, demo_daily_jobs=2))
    try:
        r = post(adapter, {"service": "echo", "input": "after restart"}, {"X-Real-IP": "203.0.113.50"})
        assert r.status_code == 429 and len(chain.included) == 2
    finally:
        stop()


def test_the_caps_are_kept_in_the_ledger(tmp_path):
    led = books.Ledger(tmp_path / "a.db")
    for _ in range(3):
        led.reserve_demo("caller-a", "echo", per_caller=3, window=600, daily=30, now=1_000_000.0)
    with pytest.raises(books.DemoLimit) as e:
        led.reserve_demo("caller-a", "echo", per_caller=3, window=600, daily=30, now=1_000_100.0)
    assert e.value.kind == "caller" and e.value.retry_after == 500
    again = books.Ledger(tmp_path / "a.db")  # another process, or the same shop after a restart
    with pytest.raises(books.DemoLimit):
        again.reserve_demo("caller-a", "echo", per_caller=3, window=600, daily=30, now=1_000_200.0)
    assert again.reserve_demo("caller-a", "echo", per_caller=3, window=600, daily=30, now=1_000_601.0)
    with pytest.raises(books.DemoLimit) as e:
        again.reserve_demo("caller-b", "echo", per_caller=3, window=600, daily=4, now=1_000_700.0)
    assert e.value.kind == "daily" and e.value.retry_after == 86400 - 1_000_700 % 86400
    assert again.demo_used_today(now=1_000_700.0) == 4 and again.demo_salt() == led.demo_salt()


def test_parallel_requests_cannot_slip_under_the_daily_cap(tmp_path):
    led = books.Ledger(tmp_path / "b.db")
    got, refused = [], []

    def go(i):
        try:
            got.append(led.reserve_demo(f"caller-{i}", "echo", per_caller=3, window=600, daily=5))
        except books.DemoLimit:
            refused.append(i)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(got) == 5 and len(refused) == 15


def test_the_demo_refuses_mainnet_and_the_shops_own_accounts():
    def demo_for(network="near:testnet", **over):
        s = {"pay_to": "shop.testnet", "network": network, "near_relayer_account": "relay.testnet",
             "services": [{"id": "echo", "title": "Echo", "price": "$0.05", "instructions": "x"}],
             "demo_buyer_account": "demo.testnet", **over}
        return demo_mod.load(as_settings(s), shop_mod.load_shop(as_settings(s)), SECRETS.get)

    assert demo_for().enabled
    off = demo_for("near:mainnet", pay_to="shop.near", near_relayer_account="relay.near", demo_buyer_account="demo.near")
    assert not off.enabled and "near:testnet only" in off.off
    assert not demo_for("eip155:84532", pay_to="0x" + "ab" * 20).enabled
    assert "not the shop's pay_to" in demo_for(demo_buyer_account="shop.testnet").off
    assert "near_relayer_account" in demo_for(demo_buyer_account="relay.testnet").off
    assert "buyer_near_account" in demo_for(buyer_near_account="demo.testnet").off
    assert "EMPORIUM_DEMO_BUYER_KEY" in demo_mod.load(
        as_settings({"network": NET, "demo_buyer_account": "demo.testnet"}),
        shop_mod.load_shop(as_settings({"pay_to": "shop.testnet", "network": NET})), lambda n: None).off
    assert not demo_for(demo_buyer_account="Not An Account").enabled


def test_a_mainnet_shop_never_opens_the_demo(chain, monkeypatch):
    monkeypatch.setitem(shop_mod.RPC_URLS, "near:mainnet", chain.url)
    adapter, stop = start_shop(settings(chain, network="near:mainnet", rpc_url=chain.url))
    try:
        assert not adapter.demo.demo.enabled
        assert "demo" not in catalog(adapter)
        r = post(adapter, {"service": "echo", "input": "hi"})
        assert r.status_code == 403 and not adapter.stub.events and chain.included == []
    finally:
        stop()


def test_a_job_that_fails_is_not_paid_and_still_counts(demo_shop):
    demo_shop.stub.fail = True
    r = post(demo_shop, {"service": "echo", "input": "hi"})
    assert r.status_code == 502 and r.json()["charged"] is False and "nothing was paid" in r.json()["error"]
    assert demo_shop.chain.included == [] and demo_shop.chain.usdc("demo.testnet") == 2_000_000
    assert demo_shop.books.demo_used_today() == 1, "the job ran, so it used a demo job"


def test_an_empty_demo_wallet_or_a_busy_demo_uses_no_demo_job(demo_shop, monkeypatch):
    demo_shop.chain.balances["demo.testnet"] = 10_000  # $0.01: less than the job
    r = post(demo_shop, {"service": "echo", "input": "hi"})
    assert r.status_code == 503 and "out of testnet USDC" in r.json()["error"]
    demo_shop.chain.balances["demo.testnet"] = 2_000_000
    monkeypatch.setattr(demo_shop.demo, "_slots", threading.BoundedSemaphore(1))
    demo_shop.demo._slots.acquire()
    r = post(demo_shop, {"service": "echo", "input": "hi"})
    assert r.status_code == 503 and r.headers["Retry-After"] == "60"
    assert demo_shop.books.demo_used_today() == 0 and not demo_shop.stub.events


def test_the_caller_address(monkeypatch):
    def h(peer, **headers):
        return SimpleNamespace(client_address=(peer, 1234), headers=httpx.Headers(headers))

    # a caller reaching the shop directly can't name another address
    assert demo_mod.caller_address(h("8.8.4.4", **{"X-Real-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"})) == "8.8.4.4"
    # behind a proxy (a private peer), the proxy's headers name the caller; the last X-Forwarded-For hop is the proxy's own
    assert demo_mod.caller_address(h("10.0.0.2", **{"X-Real-IP": "198.51.100.1"})) == "198.51.100.1"
    assert demo_mod.caller_address(h("100.64.3.4", **{"X-Forwarded-For": "6.6.6.6, 198.51.100.2"})) == "198.51.100.2"
    assert demo_mod.caller_address(h("10.0.0.2", **{"X-Real-IP": "not an ip"})) == "10.0.0.2"
    # IPv6 callers are counted per /64
    assert demo_mod.caller_address(h("2001:db8:1:2:aaaa::1")) == demo_mod.caller_address(h("2001:db8:1:2:bbbb::9")) == "2001:db8:1:2::/64"


# ---- the MCP server (mcp/), driving this shop's demo and paying with @x402/near ------------------
ROOT = Path(__file__).resolve().parent.parent
MCP = ROOT / "mcp"
NODE = shutil.which("node")
needs_mcp = pytest.mark.skipif(not NODE or not (MCP / "node_modules" / "@x402" / "near").is_dir(),
                               reason="needs node and mcp/node_modules (cd mcp && npm ci)")


def mcp_call(env: dict, *calls: tuple[str, dict]) -> list[dict]:
    """Start emporium-mcp over stdio, do the handshake, call each tool in turn, return the results."""
    msgs = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"}]
    proc = subprocess.Popen([NODE, str(MCP / "src" / "main.js")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env={**os.environ, **env})
    try:
        for m in msgs:
            proc.stdin.write(json.dumps(m) + "\n")
        proc.stdin.flush()
        results = []
        want = 0
        for i, (name, args) in enumerate(calls, start=1):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                         "params": {"name": name, "arguments": args}}) + "\n")
            proc.stdin.flush()
            want = i
            while True:
                line = proc.stdout.readline()
                assert line, proc.stderr.read()[-2000:]
                msg = json.loads(line)
                if msg.get("id") == want:
                    results.append(msg["result"])
                    break
        return results
    finally:
        proc.kill()
        proc.wait(10)


def text_of(result: dict) -> str:
    return "\n".join(c["text"] for c in result["content"])


@needs_mcp
def test_the_mcp_server_hires_through_the_demo_and_checks_the_receipt(demo_shop):
    env = {"EMPORIUM_SHOP_URL": demo_shop.test_url, "EMPORIUM_NEAR_RPC": demo_shop.chain.url,
           "EMPORIUM_NEAR_ARCHIVAL_RPC": "", "EMPORIUM_NEAR_ACCOUNT": "", "EMPORIUM_NEAR_KEY": ""}
    shop_r, hire_r = mcp_call(env, ("emporium_shop", {}), ("emporium_hire", {"service": "echo", "input": "via mcp"}))
    assert "Keyless demo: on" in text_of(shop_r) and "- echo ($0.05)" in text_of(shop_r)
    assert not hire_r.get("isError"), text_of(hire_r)
    s = hire_r["structuredContent"]
    assert s["output"] == "ECHO: via mcp" and s["paid_with"] == "keyless demo" and s["output_sha256_matches"] is True
    assert s["receipt"]["payer"] == "demo.testnet" and s["onchain"]["confirmed"] is True
    assert "On chain: confirmed. $0.05 in USDC reached shop.testnet" in text_of(hire_r)
    (verify_r,) = mcp_call(env, ("emporium_verify", {"tx": s["receipt"]["tx"]}))
    assert verify_r["structuredContent"]["confirmed"] is True and verify_r["structuredContent"]["signer"] == "relay.testnet"
    assert "0.05 USDC from demo.testnet to shop.testnet" in text_of(verify_r)


@needs_mcp
def test_the_mcp_server_pays_from_its_own_account_with_the_official_near_client(demo_shop):
    net = demo_shop.chain
    env = {"EMPORIUM_SHOP_URL": demo_shop.test_url, "EMPORIUM_NEAR_RPC": net.url, "EMPORIUM_NEAR_ARCHIVAL_RPC": "",
           "EMPORIUM_NEAR_ACCOUNT": "buyer.testnet", "EMPORIUM_NEAR_KEY": BUYER.secret()}
    (hire_r,) = mcp_call(env, ("emporium_hire", {"service": "echo", "input": "signed by @x402/near"}))
    assert not hire_r.get("isError"), text_of(hire_r)
    s = hire_r["structuredContent"]
    assert s["paid_with"] == "own account" and s["output"] == "ECHO: signed by @x402/near"
    assert s["receipt"]["payer"] == "buyer.testnet" and s["onchain"]["confirmed"] is True
    assert net.usdc("buyer.testnet") == 1_950_000 and net.usdc("shop.testnet") == 50_000
    assert demo_shop.books.demo_used_today() == 0, "paying from its own account doesn't touch the demo"
    assert BUYER.secret() not in json.dumps(hire_r)
    # a job above the server's own limit is refused before anything is signed
    (pricey,) = mcp_call({**env, "EMPORIUM_MAX_PRICE": "$1.00"}, ("emporium_hire", {"service": "pricey", "input": "x"}))
    assert pricey["isError"] and "above this server's $1.00 per-job limit" in text_of(pricey)
    assert len(net.included) == 1
