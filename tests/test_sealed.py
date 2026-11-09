"""The shop opens only on a sealed profile, and a model-supplied shop URL must be a host it's safe to call."""

from __future__ import annotations

import asyncio
import socket
import threading

import pytest

from conftest import SEALED_CONFIG, SHOP_ADDR, settings_for
from emporium import buyer, commands
from emporium.buyer import HireError, check_host
from emporium.shop import profile_risks

# ── the shop's profile ────────────────────────────────────────────────────────────────────────────


def test_a_sealed_profile_has_no_risks(hermes_home):
    assert profile_risks("shop") == []


def test_built_in_memory_keeps_the_shop_closed(hermes_home):
    (hermes_home / "config.yaml").write_text("model:\n  default: x\n")  # memory on, as Hermes defaults it
    (hermes_home / "memories").mkdir()
    (hermes_home / "memories" / "MEMORY.md").write_text("Owner's bank is Zenith; PIN hint: cat's birthday")
    (risk,) = profile_risks("shop")
    assert "memory.memory_enabled" in risk and "MEMORY.md" in risk and "background review" in risk
    assert "Zenith" not in risk  # the risk names the file, never its contents


def test_a_memory_provider_and_the_main_profile_are_risks(hermes_home):
    (hermes_home / "config.yaml").write_text(SEALED_CONFIG + "  provider: honcho\n")
    assert any("memory provider 'honcho'" in r for r in profile_risks("shop"))
    assert any("main profile" in r for r in profile_risks("default"))


def test_a_persona_cloned_from_the_main_profile_is_a_risk(tmp_path, monkeypatch):
    root = tmp_path / "root"
    shop_home = root / "profiles" / "shop"
    shop_home.mkdir(parents=True)
    (root / "config.yaml").write_text("model:\n  default: x\n")
    (shop_home / "config.yaml").write_text(SEALED_CONFIG)
    monkeypatch.setenv("HERMES_HOME", str(shop_home))
    persona = "You are Bryony's assistant. She lives in Lagos and banks with Zenith."
    (root / "SOUL.md").write_text(persona)
    (shop_home / "SOUL.md").write_text(persona + "\n")  # what --clone copies
    assert any("SOUL.md is your main profile's persona" in r for r in profile_risks("shop"))
    (shop_home / "SOUL.md").write_text("You are Ada, who runs a translation shop.")  # written for the shop
    assert profile_risks("shop") == []
    from hermes_cli.default_soul import DEFAULT_SOUL_MD
    (root / "SOUL.md").write_text(DEFAULT_SOUL_MD)
    (shop_home / "SOUL.md").write_text(DEFAULT_SOUL_MD)  # both stock: nothing personal to leak
    assert profile_risks("shop") == []


def test_project_context_files_in_the_gateway_cwd_are_a_risk(hermes_home, tmp_path, monkeypatch):
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("Deploy keys live in ~/secrets; staging password is hunter2.")
    monkeypatch.chdir(proj)
    (risk,) = profile_risks("shop")
    assert "AGENTS.md" in risk and "terminal.cwd" in risk and "hunter2" not in risk


def test_a_check_that_cannot_run_keeps_the_shop_closed(hermes_home, monkeypatch):
    import hermes_cli.config as hc

    def boom():
        raise OSError("disk")
    monkeypatch.setattr(hc, "load_config_readonly", boom)
    assert any("couldn't check" in r for r in profile_risks("shop"))


def _connect(settings: dict) -> tuple[bool, object]:
    from gateway.config import PlatformConfig
    from gateway.platform_registry import PlatformEntry, platform_registry
    from emporium.adapter import EmporiumAdapter
    if "emporium" not in platform_registry.registered_names():
        platform_registry.register(PlatformEntry(name="emporium", label="Emporium", adapter_factory=lambda c: None,
                                                 check_fn=lambda: True))
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    adapter = EmporiumAdapter(PlatformConfig(enabled=True), lambda k, d=None: settings.get(k, d), "shop")
    ok = asyncio.run_coroutine_threadsafe(adapter.connect(), loop).result(30)
    if ok:
        asyncio.run_coroutine_threadsafe(adapter.disconnect(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    return ok, adapter


def test_the_shop_refuses_to_open_on_an_unsealed_profile(hermes_home, facilitator):
    (hermes_home / "config.yaml").write_text("model:\n  default: x\n")
    ok, adapter = _connect(settings_for(facilitator.url))
    assert ok is False and adapter._httpd is None  # nothing listens, nobody can pay
    assert adapter.fatal_error_code == "profile_not_sealed"
    assert "memory.memory_enabled" in adapter.fatal_error_message and "allow_unsealed_profile" in adapter.fatal_error_message
    # the owner's explicit opt-in opens it (and the log keeps warning)
    ok, _ = _connect(settings_for(facilitator.url, allow_unsealed_profile=True))
    assert ok is True


def test_init_seals_the_shops_profile_but_never_the_main_one(hermes_home, capsys):
    (hermes_home / "config.yaml").write_text("model:\n  default: x\n")
    assert profile_risks("shop")
    stored: dict = {}

    class Ctx:
        def __init__(self, profile):
            self.profile_name = profile

        def get_config(self, k, default=None):
            return stored.get(k, default)

        def set_config(self, k, v):
            stored[k] = v

    class Args:
        emporium_command, pay_to, name, network, port = "init", SHOP_ADDR, None, None, None

    _setup, handle = commands.make_cli(Ctx("default"))
    handle(Args())
    assert "won't open here" in capsys.readouterr().out
    assert profile_risks("shop"), "init on the main profile leaves the owner's memory alone"
    _setup, handle = commands.make_cli(Ctx("shop"))
    handle(Args())
    assert "turned off memory.memory_enabled and memory.user_profile_enabled" in capsys.readouterr().out
    assert profile_risks("shop") == []


# ── the shop URLs a model asks to browse or hire ───────────────────────────────────────────────────


@pytest.fixture
def dns(monkeypatch):
    """Names resolve from this table; IP literals resolve as themselves."""
    table: dict[str, list[str]] = {}
    real = socket.getaddrinfo

    def fake(host, port, *a, **k):
        if host in table:
            return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                    for ip in table[host]]
        try:
            import ipaddress
            ipaddress.ip_address(host)
            return real(host, port, *a, **k)
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
    monkeypatch.setattr(buyer.socket, "getaddrinfo", fake)
    return table


def test_public_shops_pass(dns, monkeypatch):
    import tools.url_safety as us
    seen = []
    monkeypatch.setattr(us, "is_safe_url", lambda u: seen.append(u) or True)  # Hermes's guard is consulted too
    dns["shop.example.com"] = ["93.184.216.34"]
    assert check_host("https://shop.example.com/") == "https://shop.example.com"
    assert check_host("8.8.8.8:9402") == "http://8.8.8.8:9402"
    assert seen == ["https://shop.example.com", "http://8.8.8.8:9402"]
    monkeypatch.setattr(us, "is_safe_url", lambda u: False)
    with pytest.raises(HireError, match="URL safety check"):
        check_host("https://shop.example.com")


@pytest.mark.parametrize("url", ["http://user:pw@shop.example.com", "http://shop.example.com@127.0.0.1",
                                 "http://127.0.0.1\\@shop.example.com"])
def test_urls_with_credentials_or_backslashes_are_refused(dns, url):
    dns["shop.example.com"] = ["93.184.216.34"]
    with pytest.raises(HireError):
        check_host(url, allow=["127.0.0.1"])


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data", "http://metadata.google.internal",
                                 "http://[fd00:ec2::254]", "http://100.100.100.200", "http://[fe80::1]:80",
                                 "http://0.0.0.0:9402", "http://224.0.0.1"])
def test_link_local_and_metadata_hosts_are_always_refused(dns, url):
    with pytest.raises(HireError, match="never hired there"):
        check_host(url, allow=["169.254.169.254", "metadata.google.internal", "[fd00:ec2::254]", "100.100.100.200",
                               "[fe80::1]:80", "0.0.0.0:9402", "224.0.0.1"])


@pytest.mark.parametrize("url", ["http://127.0.0.1:9402", "http://localhost:9402", "http://10.0.0.5",
                                 "http://192.168.1.20:8080", "http://[::1]:9402", "http://100.64.3.4"])
def test_private_hosts_need_the_owners_allowlist(dns, url):
    dns["localhost"] = ["127.0.0.1", "::1"]
    with pytest.raises(HireError, match="buyer_allow_hosts"):
        check_host(url)


def test_the_allowlist_matches_host_or_host_and_port(dns):
    dns["localhost"] = ["127.0.0.1"]
    assert check_host("http://127.0.0.1:9402", ["127.0.0.1:9402"])
    assert check_host("http://127.0.0.1:9403", ["127.0.0.1"])
    assert check_host("http://localhost:9402", ["http://localhost:9402/"])
    with pytest.raises(HireError):
        check_host("http://127.0.0.1:9999", ["127.0.0.1:9402"])


def test_a_name_that_resolves_to_a_private_address_is_refused(dns):
    dns["shop.attacker.example"] = ["93.184.216.34", "127.0.0.1"]  # one bad answer is enough
    with pytest.raises(HireError, match="private address"):
        check_host("http://shop.attacker.example")
    with pytest.raises(HireError, match="couldn't resolve"):
        check_host("http://no-such-shop.invalid")


def test_the_tools_refuse_a_private_shop_before_fetching_anything(shop, monkeypatch):
    from emporium.tools import make_handlers
    settings: dict = {"buyer_max_per_job": "$1.00"}

    class Ctx:
        def get_config(self, k, default=None):
            return settings.get(k, default)

    fetched = []
    monkeypatch.setattr(buyer.httpx, "get", lambda *a, **k: fetched.append(a) or (_ for _ in ()).throw(AssertionError))
    browse, hire, gate = make_handlers(Ctx())
    buyer._catalog_cache.clear()
    assert "buyer_allow_hosts" in browse({"shop_url": shop.test_url})
    assert "buyer_allow_hosts" in hire({"shop_url": shop.test_url, "service": "echo", "input": "x"})
    blocked = gate(tool_name="emporium_hire", args={"shop_url": "http://169.254.169.254", "service": "x"})
    assert blocked["action"] == "block" and "never hired there" in blocked["message"]
    assert fetched == []
