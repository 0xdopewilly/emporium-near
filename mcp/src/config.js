// What emporium-mcp knows about NEAR, and how it is configured (all from the environment; see README.md).

export const VERSION = "0.1.0";

/** Ada's Emporium on NEAR testnet: the live shop the tools use when no `url` is given. */
export const LIVE_SHOP = "https://shop-production-8cc3cf57.up.railway.app";

export const NETWORKS = {
  "near:testnet": {
    label: "NEAR testnet",
    usdc: "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af",
    rpc: ["https://test.rpc.fastnear.com", "https://near-testnet.drpc.org"],
    archival: "https://archival-rpc.testnet.fastnear.com",
    explorer: "https://testnet.nearblocks.io/txns/",
  },
  "near:mainnet": {
    label: "NEAR mainnet",
    usdc: "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",
    rpc: ["https://free.rpc.fastnear.com", "https://near.drpc.org"],
    archival: "https://archival-rpc.mainnet.fastnear.com",
    explorer: "https://nearblocks.io/txns/",
  },
};

const ACCOUNT = /^(?=.{2,64}$)(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$/;
export const isAccountId = (s) => typeof s === "string" && ACCOUNT.test(s);

/** "$0.25" / "0.25" -> 250000n (USDC has 6 decimals); null when it isn't a positive amount. */
export function toAtomic(value) {
  const text = String(value ?? "").trim().replace(/^\$/, "").replace(/,/g, "");
  if (!/^\d+(\.\d+)?$/.test(text)) return null;
  const [whole, frac = ""] = text.split(".");
  const atomic = BigInt(whole) * 1_000_000n + BigInt((frac + "000000").slice(0, 6));
  return atomic > 0n ? atomic : null;
}

/** 250000n -> "$0.25" (at least two decimals). */
export function usd(atomic) {
  const a = BigInt(atomic);
  const whole = a / 1_000_000n;
  let frac = (a % 1_000_000n).toString().padStart(6, "0").replace(/0+$/, "");
  if (frac.length < 2) frac = frac.padEnd(2, "0");
  return `$${whole}.${frac}`;
}

export class ConfigError extends Error {}

/**
 * The server's settings from the environment:
 * - EMPORIUM_SHOP_URL: the shop the tools use when no url is given (default: the live testnet shop);
 * - EMPORIUM_NEAR_ACCOUNT + EMPORIUM_NEAR_KEY: pay from your own NEAR account instead of the shop's keyless demo;
 * - EMPORIUM_NETWORK: near:testnet (default) or near:mainnet, which must be set explicitly to spend real USDC;
 * - EMPORIUM_MAX_PRICE: the most one job may cost when paying from your own account (default $0.50);
 * - EMPORIUM_NEAR_RPC / EMPORIUM_NEAR_ARCHIVAL_RPC: other RPC endpoints (comma-separated; "" turns archival off).
 */
export function configFromEnv(env = process.env) {
  const get = (k) => (env[k] ?? "").trim();
  const network = get("EMPORIUM_NETWORK") || "near:testnet";
  if (!NETWORKS[network]) throw new ConfigError(`EMPORIUM_NETWORK must be near:testnet or near:mainnet, not ${JSON.stringify(network)}`);
  const account = get("EMPORIUM_NEAR_ACCOUNT");
  const key = get("EMPORIUM_NEAR_KEY");
  let wallet = null;
  if (account || key) {
    const missing = [!account && "EMPORIUM_NEAR_ACCOUNT", !key && "EMPORIUM_NEAR_KEY"].filter(Boolean);
    if (missing.length) throw new ConfigError(`set both EMPORIUM_NEAR_ACCOUNT and EMPORIUM_NEAR_KEY to pay from your own account (missing: ${missing.join(", ")}), or neither to use the shop's keyless demo`);
    if (!isAccountId(account)) throw new ConfigError(`EMPORIUM_NEAR_ACCOUNT ${JSON.stringify(account)} is not a NEAR account id`);
    if (!/^ed25519:[1-9A-HJ-NP-Za-km-z]{32,90}$/.test(key)) throw new ConfigError("EMPORIUM_NEAR_KEY must be the account's full-access ed25519 key (ed25519:…)");
    wallet = { account, key };
  }
  const maxPrice = toAtomic(get("EMPORIUM_MAX_PRICE") || "$0.50");
  if (maxPrice === null) throw new ConfigError("EMPORIUM_MAX_PRICE must be a USD amount, like $0.50");
  const rpc = env.EMPORIUM_NEAR_RPC !== undefined && get("EMPORIUM_NEAR_RPC") ? get("EMPORIUM_NEAR_RPC").split(",").map((s) => s.trim()).filter(Boolean) : NETWORKS[network].rpc;
  const archival = env.EMPORIUM_NEAR_ARCHIVAL_RPC !== undefined ? get("EMPORIUM_NEAR_ARCHIVAL_RPC") : NETWORKS[network].archival;
  const shopUrl = normUrl(get("EMPORIUM_SHOP_URL") || LIVE_SHOP);
  return { shopUrl, network, wallet, maxPrice, rpc, archival, rpcOverridden: Boolean(get("EMPORIUM_NEAR_RPC")) };
}

export function normUrl(url) {
  let u = String(url ?? "").trim().replace(/\/+$/, "");
  if (!u) throw new ConfigError("a shop URL is required, like https://shop.example");
  if (!/^[a-z]+:\/\//i.test(u)) u = (/^(localhost|127\.|\[::1\])/.test(u) ? "http://" : "https://") + u;
  let parsed;
  try {
    parsed = new URL(u);
  } catch {
    throw new ConfigError(`${JSON.stringify(url)} is not a URL`);
  }
  if (!["http:", "https:"].includes(parsed.protocol)) throw new ConfigError(`${JSON.stringify(url)} is not an http(s) URL`);
  if (parsed.username || parsed.password) throw new ConfigError("a shop URL carries no user name or password");
  return `${parsed.protocol}//${parsed.host}${parsed.pathname.replace(/\/+$/, "")}`;
}
