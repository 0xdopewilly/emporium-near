"""The shop: what this Hermes sells, at what price, paid to which wallet, on which network.

Everything is read from the plugin's own settings (``plugins.entries.emporium.settings`` in the
profile's config.yaml). Nothing here touches the network or the agent; it is plain data + checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

DEFAULT_FACILITATOR = "https://x402.org/facilitator"
DEFAULT_PORT = 9402

# CAIP-2 network id -> (label, explorer tx url prefix, is_testnet)
NETWORKS: dict[str, tuple[str, str, bool]] = {
    "eip155:84532": ("Base Sepolia", "https://sepolia.basescan.org/tx/", True),
    "eip155:8453": ("Base", "https://basescan.org/tx/", False),
}

# A service may hand its job these tools, and only these. Everything else is refused for shop jobs
# (see guard.py). Read-only web tools are the useful set; nothing here touches the owner's machine.
SELLABLE_TOOLS = frozenset({"web_search", "web_extract", "vision_analyze", "image_generate"})

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
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


def to_atomic(amount: Decimal, decimals: int = 6) -> int:
    """USD amount -> USDC base units (6 decimals)."""
    return int((amount * (Decimal(10) ** decimals)).to_integral_value())


def from_atomic(value: Any, decimals: int = 6) -> Decimal:
    try:
        return Decimal(int(value)) / (Decimal(10) ** decimals)
    except (TypeError, ValueError):
        return Decimal(0)


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
    max_concurrent_jobs: int = 3
    jobs_per_payer_per_hour: int = 30
    problems: list[str] = field(default_factory=list)

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
        return {
            "emporium": "1",
            "name": self.name,
            "tagline": self.tagline,
            "open": self.open,
            "pay_to": self.pay_to,
            "network": self.network,
            "network_label": self.network_label,
            "asset": "USDC",
            "protocol": "x402",
            "x402_version": 2,
            "charged": "only on delivery: the payment is verified before work starts and settled after the result is ready",
            "services": [s.public(base, self.network) for s in self.services],
        }


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
    )


def load_shop(get: Callable[[str, Any], Any], profile: str = "") -> Shop:
    """Build the Shop from settings. ``get(key, default)`` is ``ctx.get_config`` (or a dict lookup in tests)."""
    problems: list[str] = []
    pay_to = str(get("pay_to", "") or "").strip()
    if not _ADDRESS.match(pay_to):
        problems.append("pay_to must be the 0x… wallet address that receives your earnings")
    network = str(get("network", "eip155:84532") or "eip155:84532").strip()
    if network not in NETWORKS:
        problems.append(f"network {network!r} is not supported (use one of {sorted(NETWORKS)})")
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
    default_name = f"{profile.capitalize()}'s Emporium" if profile and profile != "default" else "Hermes Emporium"

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
        max_concurrent_jobs=_int("max_concurrent_jobs", 3, 1, 32),
        jobs_per_payer_per_hour=_int("jobs_per_payer_per_hour", 30, 1, 10000),
        problems=problems,
    )
