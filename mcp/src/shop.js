// Talking to an Emporium shop: its catalog, its keyless demo, and a paid hire over x402 `exact` on NEAR
// from your own account, signed with the official @x402/near client scheme.
import { createHash } from "node:crypto";
import { NETWORKS, toAtomic, usd } from "./config.js";

const BUSY_RETRIES = 3; // a shop answering 503 (all hands busy) before any payment is asked again, a few seconds apart
export const SETTLE_WINDOW = 180; // seconds a paid job may take beyond the service's own timeout: settling on chain

/** A refusal the agent can act on. `status` is the shop's HTTP status, when it answered. */
export class ShopError extends Error {
  constructor(message, { status, data } = {}) {
    super(message);
    this.name = "ShopError";
    this.status = status;
    this.data = data;
  }
}

async function readJson(res) {
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch {
    return { error: text.slice(0, 300) };
  }
}

export async function catalog(url, { fetch = globalThis.fetch } = {}) {
  let res;
  try {
    res = await fetch(`${url}/.well-known/emporium.json`, { signal: AbortSignal.timeout(20_000), redirect: "error" });
  } catch (e) {
    throw new ShopError(`couldn't reach a shop at ${url} (${e?.cause?.code ?? e?.name ?? e})`);
  }
  if (res.status !== 200) throw new ShopError(`${url} has no Emporium catalog (HTTP ${res.status})`, { status: res.status });
  const cat = await readJson(res);
  if (!cat || typeof cat !== "object" || !Array.isArray(cat.services)) throw new ShopError(`${url} returned something that isn't an Emporium catalog`);
  return cat;
}

/** The shop's own numbers (its job feed); null when it publishes none. */
export async function feed(url, { fetch = globalThis.fetch } = {}) {
  try {
    const res = await fetch(`${url}/feed`, { signal: AbortSignal.timeout(15_000), redirect: "error" });
    return res.status === 200 ? await res.json() : null;
  } catch {
    return null;
  }
}

export function listed(cat, serviceId) {
  const svc = cat.services.find((s) => s.id === serviceId);
  if (!svc) throw new ShopError(`${cat.name ?? "The shop"} has no service ${JSON.stringify(serviceId)}; it sells: ${cat.services.map((s) => s.id).join(", ")}`);
  const price = toAtomic(svc.price_usd ?? svc.price);
  if (price === null) throw new ShopError("the shop lists no valid price for that service");
  return { svc, price };
}

const jobTimeoutMs = (svc) => (Number(svc?.timeout_seconds) || 300) * 1000 + SETTLE_WINDOW * 1000;

/**
 * Hire through the shop's keyless demo: POST /demo/hire with the service and the input, nothing else.
 * The shop's own demo account pays, the shop's relayer settles after delivery.
 */
export async function demoHire(url, cat, serviceId, input, { fetch = globalThis.fetch, signal } = {}) {
  const { svc } = listed(cat, serviceId);
  const endpoint = cat.demo?.endpoint && sameOrigin(cat.demo.endpoint, url) ? cat.demo.endpoint : `${url}/demo/hire`;
  let res;
  try {
    res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ service: serviceId, input }),
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(jobTimeoutMs(svc))]) : AbortSignal.timeout(jobTimeoutMs(svc)),
      redirect: "error",
    });
  } catch (e) {
    if (e?.name === "TimeoutError") throw new ShopError("the shop didn't answer in time; the job may still finish on its side");
    throw new ShopError(`couldn't reach the shop's demo at ${endpoint} (${e?.cause?.code ?? e?.name ?? e})`);
  }
  const data = await readJson(res);
  if (res.status !== 200 || typeof data?.output !== "string") throw new ShopError(String(data?.error ?? `HTTP ${res.status}`), { status: res.status, data });
  return { ...data, endpoint };
}

function sameOrigin(a, b) {
  try {
    return new URL(a).origin === new URL(b).origin;
  } catch {
    return false;
  }
}

// One NEAR key's nonces must land in order, so this process pays for one job at a time.
let queue = Promise.resolve();
function oneAtATime(fn) {
  const run = queue.then(fn, fn);
  queue = run.catch(() => {});
  return run;
}

/**
 * Hire and pay from your own NEAR account: the shop answers 402, the official @x402/near client signs a
 * NEP-366 delegate holding one USDC ft_transfer, the shop does the job and its relayer submits the
 * transfer after delivery. Nothing is signed unless the 402 asks for the catalog's price (at most
 * maxPrice), in USDC, to the catalog's pay_to, on this server's network.
 */
export async function paidHire(url, cat, serviceId, input, { wallet, network, maxPrice, rpcUrl, fetch = globalThis.fetch, signal, onProgress = () => {} }) {
  const { svc, price } = listed(cat, serviceId);
  const net = NETWORKS[network];
  if (cat.network !== network) {
    throw new ShopError(`${cat.name ?? "this shop"} is paid on ${cat.network}, and this server pays on ${network}${cat.network === "near:mainnet" ? " (set EMPORIUM_NETWORK=near:mainnet to spend real USDC there)" : ""}`);
  }
  if (price > maxPrice) throw new ShopError(`${serviceId} costs ${usd(price)}, above this server's ${usd(maxPrice)} per-job limit (EMPORIUM_MAX_PRICE)`);
  const payTo = String(cat.pay_to ?? "");
  const endpoint = `${url}/jobs/${encodeURIComponent(serviceId)}`;
  const body = JSON.stringify({ input });
  const timeout = () => (signal ? AbortSignal.any([signal, AbortSignal.timeout(jobTimeoutMs(svc))]) : AbortSignal.timeout(jobTimeoutMs(svc)));
  const post = async (headers = {}) => {
    for (let attempt = 0; ; attempt++) {
      const res = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body, signal: timeout(), redirect: "error" });
      if (res.status !== 503 || attempt === BUSY_RETRIES) return res;
      await res.body?.cancel();
      await new Promise((r) => setTimeout(r, 2000 + 2000 * attempt));
    }
  };

  const [{ x402Client, x402HTTPClient }, { ExactNearScheme }, { createClientNearSigner }] = await Promise.all([
    import("@x402/core/client"),
    import("@x402/near/exact/client"),
    import("@x402/near"),
  ]);
  const signer = createClientNearSigner({ accountId: wallet.account, secretKey: wallet.key, rpcUrls: { [network]: rpcUrl } });
  const client = new x402Client().register(network, new ExactNearScheme(signer));
  client.setSpendControls({ maxAmountPerPayment: usd(maxPrice < price ? maxPrice : price) });
  const http = new x402HTTPClient(client);

  const first = await post();
  if (first.status !== 402) {
    const data = await readJson(first);
    throw new ShopError(`the shop refused before any payment (HTTP ${first.status}): ${data?.error ?? ""}`, { status: first.status, data });
  }
  let required;
  try {
    required = http.getPaymentRequiredResponse((n) => first.headers.get(n), await readJson(first));
  } catch (e) {
    throw new ShopError(`the shop's 402 isn't valid x402: ${e.message}`);
  }
  const ok = (required.accepts ?? []).filter(
    (r) => r.scheme === "exact" && r.network === network && r.payTo === payTo && r.asset === net.usdc && /^\d+$/.test(String(r.amount)) && BigInt(r.amount) <= price,
  );
  if (!ok.length) {
    const asked = (required.accepts ?? []).map((r) => `${r.amount} of ${r.asset} on ${r.network} to ${r.payTo}`).join("; ");
    throw new ShopError(`the shop's payment request (${asked}) doesn't match its listed ${usd(price)} in USDC to ${payTo}; nothing was signed`);
  }
  required.accepts = ok.slice(0, 1);
  const charged = BigInt(ok[0].amount);

  return oneAtATime(async () => {
    let paid;
    const started = Date.now();
    try {
      const payload = await http.createPaymentPayload(required);
      onProgress(`Signed a ${usd(charged)} USDC transfer to ${payTo}; ${cat.name ?? "the shop"} is doing the job (it submits the transfer only after delivering)`);
      paid = await post(http.encodePaymentSignatureHeader(payload));
    } catch (e) {
      if (e?.name === "TimeoutError") throw new ShopError("the shop didn't answer in time; the payment may still settle if it delivers (check your account before trying again)");
      throw new ShopError(`the paid request failed before delivery: ${e?.message ?? e}`);
    }
    const data = await readJson(paid);
    if (paid.status !== 200 || typeof data?.output !== "string") {
      const err = String(data?.error ?? `HTTP ${paid.status}`);
      throw new ShopError(`no delivery (HTTP ${paid.status}): ${err}${/not charged/.test(err) ? "" : "; you were not charged"}`, { status: paid.status, data });
    }
    let tx = "";
    try {
      tx = http.getPaymentSettleResponse((n) => paid.headers.get(n))?.transaction ?? "";
    } catch {
      tx = String(data.receipt?.tx ?? "");
    }
    const receipt = { ...(data.receipt ?? {}), tx: tx || String(data.receipt?.tx ?? ""), paid: usd(charged), seconds: Math.round((Date.now() - started) / 100) / 10 };
    if (receipt.tx) receipt.explorer = net.explorer + receipt.tx;
    return { shop: cat.name, service: serviceId, title: svc.title, job: data.job ?? "", output: data.output, receipt };
  });
}

export const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");
