"""register() against a recording context: every surface the manifest declares is wired, and the
hire gate blocks or escalates according to the owner's limits."""

from conftest import SHOP_ADDR

import emporium
from emporium import buyer


class Ctx:
    profile_name = "ada"

    def __init__(self, settings):
        self.settings = settings
        self.tools, self.hooks, self.commands, self.cli, self.platforms = {}, {}, {}, {}, []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, handler)

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)

    def register_platform(self, **kw):
        self.platforms.append(kw)


def test_register_wires_everything():
    ctx = Ctx({"pay_to": SHOP_ADDR})
    emporium.register(ctx)
    assert set(ctx.tools) == {"emporium_browse", "emporium_hire"}
    assert set(ctx.hooks) == {"pre_llm_call", "pre_tool_call", "post_api_request", "on_session_end"}
    assert "shop" in ctx.commands and "emporium" in ctx.cli
    (p,) = ctx.platforms
    assert p["name"] == "emporium" and callable(p["adapter_factory"])
    # with no menu the shop is not "connected", so a gateway never opens an empty shop
    assert p["is_connected"](None) is False


def test_hire_gate(shop):
    ctx = Ctx({"buyer_max_per_job": "$1.00", "buyer_approve_over": "$0.10"})
    emporium.register(ctx)
    gate = [h for h in ctx.hooks["pre_tool_call"] if h.__name__ == "gate"][0]
    buyer._catalog_cache.clear()
    assert gate(tool_name="terminal", args={}) is None
    # a shop on loopback is hired only once the owner lists it
    local = gate(tool_name="emporium_hire", args={"shop_url": shop.test_url, "service": "echo"})
    assert local["action"] == "block" and "buyer_allow_hosts" in local["message"]
    ctx.settings["buyer_allow_hosts"] = ["127.0.0.1"]
    assert gate(tool_name="emporium_hire", args={"shop_url": shop.test_url, "service": "echo"}) is None  # $0.05
    ask = gate(tool_name="emporium_hire", args={"shop_url": shop.test_url, "service": "search"})       # $0.10 → not above
    assert ask is None
    ctx.settings["buyer_approve_over"] = "$0.05"
    ask = gate(tool_name="emporium_hire", args={"shop_url": shop.test_url, "service": "search"})
    assert ask["action"] == "approve" and "$0.10" in ask["message"]
    block = gate(tool_name="emporium_hire", args={"shop_url": shop.test_url, "service": "pricey"})     # $3 > $1
    assert block["action"] == "block" and "Nothing was paid" in block["message"]
    assert "tell them" in block["message"], "an owner's limit is reported to the owner, not worked around"


def test_tool_lock_hook_blocks_unsold_tools():
    ctx = Ctx({})
    emporium.register(ctx)
    on_llm = [h for h in ctx.hooks["pre_llm_call"]][0]
    on_tool = [h for h in ctx.hooks["pre_tool_call"] if h.__name__ == "on_tool"][0]
    emporium.guard.SESSIONS.expect("job_bbbbbbbbbbbb", ["web_search"])
    on_llm(session_id="s9", platform="emporium", user_message="[EMPORIUM JOB job_bbbbbbbbbbbb · x]")
    assert on_tool(tool_name="web_search", session_id="s9") is None
    assert on_tool(tool_name="read_file", session_id="s9")["action"] == "block"
    assert on_tool(tool_name="read_file", session_id="owner-cli-session") is None
    emporium.guard.SESSIONS.forget("job_bbbbbbbbbbbb")
