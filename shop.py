"""The shop: what this Hermes sells, at what price, paid to which wallet, on which network.

Everything is read from the plugin's own settings (``plugins.entries.emporium.settings`` in the
profile's config.yaml). Nothing here touches the network or the agent; it is plain data + checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

DEFAULT_FACILITATOR = "https://x402.org/facilitator"
DEFAULT_PORT = 9402

# CAIP-2 network id -> (label, explorer tx url prefix, is_testnet)
NETWORKS: dict[str, tuple[str, str, bool]] = {
    "near:testnet": ("NEAR testnet", "https://testnet.nearblocks.io/txns/", True),
    "near:mainnet": ("NEAR", "https://nearblocks.io/txns/", False),
    "eip155:84532": ("Base Sepolia", "https://sepolia.basescan.org/tx/", True),
    "eip155:8453": ("Base", "https://basescan.org/tx/", False),
}
DEFAULT_NETWORK = "near:testnet"
# Public JSON-RPC endpoints and the USDC contract per network, for checking receipts on-chain.
# On NEAR, USDC is Circle's native NEP-141 token (6 decimals), at an implicit account id.
# On NEAR, a comma-separated list: a call the first endpoint refuses for its rate limit goes to the next.
RPC_URLS = {"near:testnet": "https://test.rpc.fastnear.com,https://near-testnet.drpc.org",
            "near:mainnet": "https://free.rpc.fastnear.com,https://near.drpc.org",
            "eip155:84532": "https://sepolia.base.org", "eip155:8453": "https://mainnet.base.org"}
USDC = {"near:testnet": "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af",
        "near:mainnet": "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",
        "eip155:84532": "0x036cbd53842c5426634e7929541ec2318f3dcf7e", "eip155:8453": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"}


def is_near(network: Any) -> bool:
    return str(network or "").startswith("near:")

# A service may hand its job these tools, and only these. Everything else is never loaded for a shop
# job and is refused if called (see guard.py). Only the web tools are sellable: Hermes checks every URL
# web_extract fetches against private and metadata addresses. vision_analyze and image_generate are not,
# because they accept local file paths.
SELLABLE_TOOLS = frozenset({"web_search", "web_extract"})

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
# A NEAR account id: named (ada.testnet) or implicit (64 hex). Same rule as near.ACCOUNT_ID.
_NEAR_ACCOUNT = re.compile(r"^(?=.{2,64}$)(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$")
_MAX_PRICE = Decimal("100")  # a sanity ceiling per job, in USD


def parse_usd(value: Any) -> Optional[Decimal]:
    """'$0.05' / '0.05' / 0.05 -> Decimal('0.05'); None when it is not a positive amount."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text.startswith("$"):
        text = text[1:]
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return amount


def usd(amount: Decimal) -> str:
    """Decimal('0.05') -> '$0.05' (at least two decimals, no trailing noise)."""
    q = amount.normalize()
    text = format(q, "f")
    if "." not in text:
        text += ".00"
    elif len(text.split(".")[1]) < 2:
        text += "0"
    return "$" + text


def money(amount: Decimal) -> str:
    """For display: cents, or four decimals under a cent (AI costs are often a fraction of one)."""
    d = Decimal(amount or 0)
    if d == 0:
        return "$0.00"
    sign = "−" if d < 0 else ""
    d = abs(d)
    if d >= Decimal("0.01"):
        return f"{sign}${d.quantize(Decimal('0.01')):,}"
    return f"{sign}${d.quantize(Decimal('0.0001'))}"


def to_atomic(amount: Decimal, decimals: int = 6) -> int:
    """USD amount -> USDC base units (6 decimals)."""
    return int((amount * (Decimal(10) ** decimals)).to_integral_value())


def from_atomic(value: Any, decimals: int = 6) -> Decimal:
    try:
        return Decimal(int(value)) / (Decimal(10) ** decimals)
    except (TypeError, ValueError):
        return Decimal(0)


@dataclass
class Subcontract:
    """Another Emporium shop a service may hire during a job, pinned by the owner (never by the buyer)."""
    shop: str            # normalised base URL
    service: str
    max_price: Decimal
    times: int = 1

    @property
    def max_price_label(self) -> str:
        return usd(self.max_price)


@dataclass
class Service:
    id: str
    title: str
    price: Decimal
    description: str = ""
    instructions: str = ""
    tools: list[str] = field(default_factory=list)
    example: str = ""
    max_input_chars: int = 8000
    timeout_seconds: int = 180
    max_tool_calls: int = 8
    subcontract: list[Subcontract] = field(default_factory=list)

    @property
    def subcontract_budget(self) -> Decimal:
        return sum((p.max_price * p.times for p in self.subcontract), Decimal(0))

    @property
    def price_label(self) -> str:
        return usd(self.price)

    def public(self, base_url: str, network: str) -> dict[str, Any]:
        """What buyers see: never the private instructions."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "price": self.price_label,
            "price_usd": str(self.price),
            "network": network,
            "endpoint": f"{base_url}/jobs/{self.id}",
            "method": "POST",
            "input": {"type": "object", "properties": {"input": {"type": "string", "maxLength": self.max_input_chars}},
                      "required": ["input"]},
            "example": self.example,
            "tools": list(self.tools),
            "timeout_seconds": self.timeout_seconds,
            "subcontracts": [{"shop": p.shop, "service": p.service} for p in self.subcontract],
        }


@dataclass
class Shop:
    name: str
    tagline: str
    pay_to: str
    network: str
    facilitator_url: str
    host: str
    port: int
    public_url: str
    services: list[Service]
    open: bool = True
    profile: str = ""
    max_concurrent_jobs: int = 3
    jobs_per_payer_per_hour: int = 30
    rpc_url: str = ""
    allow_unsealed: bool = False
    near_relayer: str = ""   # NEAR only: the account whose key submits buyers' signed transfers and pays the gas
    problems: list[str] = field(default_factory=list)

    @property
    def near(self) -> bool:
        return is_near(self.network)

    @property
    def shared_profile(self) -> bool:
        """True when the shop runs on the owner's main profile, whose memory and persona every
        gateway session loads. The shop won't open there unless the owner sets allow_unsealed_profile."""
        return self.profile in ("", "default")

    @property
    def network_label(self) -> str:
        return NETWORKS.get(self.network, (self.network, "", False))[0]

    @property
    def testnet(self) -> bool:
        return NETWORKS.get(self.network, ("", "", True))[2]

    def explorer(self, tx: str) -> str:
        prefix = NETWORKS.get(self.network, ("", "", False))[1]
        return prefix + tx if prefix and tx else ""

    def base_url(self) -> str:
        if self.public_url:
            return self.public_url.rstrip("/")
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host
        return f"http://{host}:{self.port}"

    def service(self, service_id: str) -> Optional[Service]:
        return next((s for s in self.services if s.id == service_id), None)

    @property
    def ready(self) -> bool:
        return not self.problems

    def catalog(self) -> dict[str, Any]:
        base = self.base_url()
        cat = {
            "emporium": "1",
            "name": self.name,
            "tagline": self.tagline,
            "open": self.open,
            "pay_to": self.pay_to,
            "network": self.network,
            "network_label": self.network_label,
            "asset": "USDC",
            "asset_contract": USDC.get(self.network, ""),
            "protocol": "x402",
            "x402_version": 2,
            "charged": "only on delivery: the payment is verified before work starts and settled after the result is ready",
            "services": [s.public(base, self.network) for s in self.services],
        }
        if self.near:
            # A NEAR transaction is looked up by its hash and its signer, so receipts name the relayer.
            cat["relayer"] = self.near_relayer
            cat["gas"] = "paid by the shop's relayer: a buyer signs a USDC transfer (NEP-366) and needs no NEAR"
        return cat


SEAL_HINT = ("give the shop a fresh profile of its own (hermes profile create shop, without --clone, which copies "
             "your memory and persona), then run hermes -p shop emporium init there: it turns that profile's memory off")
UNSEALED_SETTING = "allow_unsealed_profile"
# Project context files Hermes loads into the system prompt from the agent's working directory (its
# documented discovery: .hermes.md up to the git root, the AGENTS.md chain from the git root down, and
# CLAUDE.md, .cursorrules and .cursor/rules/*.mdc in the directory itself).
_CONTEXT_WALKED = (".hermes.md", "HERMES.md", "AGENTS.override.md", "AGENTS.md", "agents.md")
_CONTEXT_HERE = ("CLAUDE.md", "claude.md", ".cursorrules")
_TRUTHY = {"1", "true", "yes", "on"}


def _nonempty(f: Path) -> bool:
    try:
        return f.is_file() and bool(f.read_text(errors="ignore").strip())
    except OSError:
        return True  # unreadable: assume it holds something


def _on(value: Any, default: bool = True) -> bool:
    """A config flag read the way Hermes reads it: unset is the default, strings must be truthy."""
    if value is None:
        return default
    return value.strip().lower() in _TRUTHY if isinstance(value, str) else bool(value)


def _soul(text: str) -> str:
    return " ".join(text.split())


def _copied_persona(home: Path) -> bool:
    """True when this profile's SOUL.md is the owner's main-profile persona (what --clone copies), rather
    than Hermes's stock persona or one written for the shop."""
    from hermes_constants import get_default_hermes_root
    root = Path(get_default_hermes_root())
    if root.resolve() == home.resolve():
        return False  # this is the main home itself; the main-profile check covers it
    mine, main = home / "SOUL.md", root / "SOUL.md"
    if not (_nonempty(mine) and _nonempty(main)):
        return False
    text = main.read_text(errors="ignore")
    if _soul(mine.read_text(errors="ignore")) != _soul(text):
        return False
    try:
        from hermes_cli.config import DEFAULT_SOUL_MD, is_legacy_template_soul
    except ImportError:
        return True  # can't tell the stock persona apart: count it
    return _soul(text) != _soul(DEFAULT_SOUL_MD) and not is_legacy_template_soul(text)


def _context_files(cfg: dict) -> tuple[Path, list[str]]:
    """Project context files Hermes would load into every job's prompt from the gateway's working directory."""
    term = cfg.get("terminal") if isinstance(cfg.get("terminal"), dict) else {}
    raw = str(term.get("cwd") or "").strip()
    if raw and raw != "." and Path(raw).expanduser().is_dir():
        cwd = Path(raw).expanduser()  # the profile's terminal.cwd, which the gateway runs jobs in
    else:
        from agent.runtime_cwd import resolve_agent_cwd  # TERMINAL_CWD in the gateway, else the launch dir
        cwd = resolve_agent_cwd()
    cwd = cwd.resolve()
    git = next((d for d in (cwd, *cwd.parents) if (d / ".git").exists()), None)
    chain = [d for d in (cwd, *cwd.parents) if d == git or (git is not None and git in d.parents)] or [cwd]
    found = [n for n in _CONTEXT_HERE if _nonempty(cwd / n)]
    rules = cwd / ".cursor" / "rules"
    if rules.is_dir() and any(_nonempty(f) for f in rules.glob("*.mdc")):
        found.append(".cursor/rules")
    found += [n if d == cwd else str(d / n) for d in chain for n in _CONTEXT_WALKED if _nonempty(d / n)]
    return cwd, found


def profile_risks(profile: str) -> list[str]:
    """What the profile serving the shop would put in front of a paying stranger, or let them write into.
    Tools are locked either way; these are the prompt and memory paths the lock can't see. The adapter
    refuses to open the shop while any of them applies, unless the owner sets allow_unsealed_profile."""
    risks: list[str] = []
    if profile in ("", "default"):
        risks.append("the shop runs on your main profile, so buyers' jobs load its persona and memory")
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.config import load_config_readonly
        home = Path(get_hermes_home())
        cfg = load_config_readonly() or {}
        mem = cfg.get("memory") if isinstance(cfg.get("memory"), dict) else {}
        mem_on, user_on = _on(mem.get("memory_enabled")), _on(mem.get("user_profile_enabled"))
        if mem_on or user_on:
            held = [n for n, on in (("MEMORY.md", mem_on), ("USER.md", user_on)) if on and _nonempty(home / "memories" / n)]
            keys = " and ".join(k for k, on in (("memory.memory_enabled", mem_on), ("memory.user_profile_enabled", user_on)) if on)
            risks.append(f"built-in memory is on ({keys}): every job's prompt would carry this profile's memory"
                         + (f" ({', '.join(held)} hold notes now)" if held else "")
                         + ", and Hermes's background review could save a buyer's text into it")
        provider = str(mem.get("provider") or "").strip()
        if provider:
            risks.append(f"memory provider '{provider}' would record every turn on this profile, buyers' jobs included")
        if profile not in ("", "default") and _copied_persona(home):
            risks.append("this profile's SOUL.md is your main profile's persona (copied by --clone), and every job's prompt would include it")
        cwd, files = _context_files(cfg)
        if files:
            risks.append(f"the gateway's working directory {cwd} has project context files ({', '.join(files)}) "
                         "that would load into every job's prompt; point terminal.cwd at an empty folder")
    except Exception as e:  # a check that can't run counts as a risk: the shop stays closed
        risks.append(f"couldn't check this profile's memory settings ({e.__class__.__name__})")
    return risks


# Starter shop written by `hermes emporium init`. Plain-language services that need no tools at all,
# plus one that reads the web, so a new shop is useful the minute it opens.
STARTER_SERVICES: list[dict[str, Any]] = [
    {
        "id": "translate-yoruba",
        "title": "English → Yoruba translation",
        "price": "$0.05",
        "description": "Natural, idiomatic Yoruba with correct tone marks. Up to ~1,500 words.",
        "instructions": ("Translate the buyer's text into natural, idiomatic Yoruba with correct tone marks. "
                         "Keep names, numbers and formatting. Reply with the translation only."),
        "example": "Good morning! Your order has shipped and will arrive on Friday.",
        "tools": [],
    },
    {
        "id": "code-review",
        "title": "Code review",
        "price": "$0.25",
        "description": "A senior reviewer's pass over a diff or file: bugs first, then risks, then nits.",
        "instructions": ("Review the buyer's code like a senior engineer. List real bugs first (with line references), "
                         "then security or reliability risks, then at most three nits. Be specific and brief. "
                         "If the code is fine, say so."),
        "example": "def add(a, b):\n    return a - b",
        "tools": [],
    },
    {
        "id": "web-brief",
        "title": "Web research brief",
        "price": "$0.40",
        "description": "Searches the live web and returns a sourced one-page brief on any question.",
        "instructions": ("Research the buyer's question on the live web. Return a tight brief: a 2-sentence answer, "
                         "5 key facts with source links, and what is still uncertain. No filler."),
        "example": "What changed in the x402 protocol v2?",
        "tools": ["web_search"],
        "timeout_seconds": 300,
    },
]


def _service_from(raw: Any, index: int, problems: list[str]) -> Optional[Service]:
    if not isinstance(raw, dict):
        problems.append(f"services[{index}] is not a mapping")
        return None
    sid = str(raw.get("id") or "").strip().lower()
    if not _ID.match(sid):
        problems.append(f"services[{index}]: id {sid!r} must be lowercase letters, digits and dashes")
        return None
    price = parse_usd(raw.get("price"))
    if price is None:
        problems.append(f"service {sid}: price {raw.get('price')!r} is not a positive USD amount")
        return None
    if price < Decimal("0.000001"):
        problems.append(f"service {sid}: price {raw.get('price')!r} is below USDC's smallest unit ($0.000001)")
        return None
    if price > _MAX_PRICE:
        problems.append(f"service {sid}: price {usd(price)} is above the {usd(_MAX_PRICE)} ceiling")
        return None
    tools = [str(t).strip() for t in (raw.get("tools") or []) if str(t).strip()]
    unsellable = [t for t in tools if t not in SELLABLE_TOOLS]
    if unsellable:
        problems.append(f"service {sid}: tools {unsellable} can't be sold (allowed: {sorted(SELLABLE_TOOLS)})")
        tools = [t for t in tools if t in SELLABLE_TOOLS]

    def _int(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(raw.get(key, default))))
        except (TypeError, ValueError):
            return default

    pins: list[Subcontract] = []
    for j, rawp in enumerate(raw.get("subcontract") or []):
        if not isinstance(rawp, dict):
            problems.append(f"service {sid}: subcontract[{j}] is not a mapping")
            continue
        from .buyer import HireError, norm_url
        try:
            url = norm_url(str(rawp.get("shop") or ""))
        except HireError as e:
            problems.append(f"service {sid}: subcontract[{j}]: {e}")
            continue
        psid = str(rawp.get("service") or "").strip().lower()
        pmax = parse_usd(rawp.get("max_price"))
        if not _ID.match(psid) or pmax is None:
            problems.append(f"service {sid}: subcontract[{j}] needs a service id and a max_price")
            continue
        try:
            times = max(1, min(5, int(rawp.get("times", 1))))
        except (TypeError, ValueError):
            times = 1
        pins.append(Subcontract(shop=url, service=psid, max_price=pmax, times=times))
    budget = sum((p.max_price * p.times for p in pins), Decimal(0))
    if pins and budget >= price:
        problems.append(f"service {sid}: subcontracts may cost up to {usd(budget)}, which must stay below its "
                        f"{usd(price)} price so every job keeps a margin")
        pins = []

    return Service(
        id=sid,
        title=str(raw.get("title") or sid).strip()[:80],
        price=price,
        description=str(raw.get("description") or "").strip()[:400],
        instructions=str(raw.get("instructions") or "").strip()[:4000],
        tools=tools,
        example=str(raw.get("example") or "").strip()[:600],
        max_input_chars=_int("max_input_chars", 8000, 100, 50000),
        timeout_seconds=_int("timeout_seconds", 180, 20, 900),
        max_tool_calls=_int("max_tool_calls", 8, 0, 50),
        subcontract=pins,
    )


def load_shop(get: Callable[[str, Any], Any], profile: str = "") -> Shop:
    """Build the Shop from settings. ``get(key, default)`` is ``ctx.get_config`` (or a dict lookup in tests)."""
    problems: list[str] = []
    network = str(get("network", DEFAULT_NETWORK) or DEFAULT_NETWORK).strip()
    if network not in NETWORKS:
        problems.append(f"network {network!r} is not supported (use one of {sorted(NETWORKS)})")
    pay_to = str(get("pay_to", "") or "").strip()
    near_relayer = ""
    if is_near(network):
        if not _NEAR_ACCOUNT.match(pay_to):
            problems.append("pay_to must be the NEAR account that receives your earnings, e.g. myshop.testnet")
        near_relayer = str(get("near_relayer_account", "") or "").strip()
        if not _NEAR_ACCOUNT.match(near_relayer):
            problems.append("near_relayer_account must be the NEAR account that submits buyers' payments and pays "
                            "their gas (its key goes in EMPORIUM_NEAR_RELAYER_KEY)")
    elif not _ADDRESS.match(pay_to):
        problems.append("pay_to must be the 0x… wallet address that receives your earnings")
    elif pay_to != pay_to.lower() and pay_to[2:] != pay_to[2:].upper():
        try:
            from eth_utils import is_checksum_address
            if not is_checksum_address(pay_to):
                problems.append("pay_to has a checksum typo (its upper/lower case letters don't match); copy it again")
        except ImportError:
            pass
    raw_services = get("services", None)
    if raw_services is None:
        raw_services = []
    services = [s for i, raw in enumerate(raw_services if isinstance(raw_services, list) else [])
                if (s := _service_from(raw, i, problems)) is not None]
    if not services:
        problems.append("no services: add at least one under services (or run `hermes emporium init`)")
    ids = [s.id for s in services]
    if len(set(ids)) != len(ids):
        problems.append("service ids must be unique")
    raw_port = get("port", DEFAULT_PORT)
    try:
        port = DEFAULT_PORT if raw_port in (None, "") else int(raw_port)  # 0 = any free port (tests)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    if not 0 <= port <= 65535:
        problems.append(f"port {port} is out of range")
    default_name = f"{profile.capitalize()}'s Emporium" if profile and profile not in ("default", "shop", "store") else "Hermes Emporium"

    def _int(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(get(key, default))))
        except (TypeError, ValueError):
            return default

    return Shop(
        name=str(get("shop_name", "") or default_name).strip()[:60],
        tagline=str(get("tagline", "") or "Skills for hire. Paid per job in USDC. You only pay when the work is delivered.").strip()[:160],
        pay_to=pay_to,
        network=network,
        facilitator_url=str(get("facilitator_url", "") or DEFAULT_FACILITATOR).strip().rstrip("/"),
        host=str(get("host", "127.0.0.1") or "127.0.0.1").strip(),
        port=port,
        public_url=str(get("public_url", "") or "").strip(),
        services=services,
        open=bool(get("open", True)),
        profile=profile or "",
        max_concurrent_jobs=_int("max_concurrent_jobs", 3, 1, 32),
        jobs_per_payer_per_hour=_int("jobs_per_payer_per_hour", 30, 1, 10000),
        rpc_url=str(get("rpc_url", "") or "").strip(),
        allow_unsealed=get(UNSEALED_SETTING, False) is True or str(get(UNSEALED_SETTING, "")).strip().lower() == "true",
        near_relayer=near_relayer,
        problems=problems,
    )
