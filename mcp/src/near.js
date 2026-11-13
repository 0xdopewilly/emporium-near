// NEAR JSON-RPC, and reading a payment receipt back from the chain. Plain fetch, no NEAR SDK.
//
// A call the first endpoint refuses for its rate limit, or can't answer, goes to the next one (FastNEAR,
// then dRPC). A transaction is looked up by its hash and the account that signed it; public nodes keep
// about the last 30 hours of them, so an older one is read from an archival node.

const RATE_LIMITED = /rate.?limit|too many requests/i;
const PLAN_LIMIT = /free plan|free tier|upgrade to (?:a )?paid|quota exceeded/i;

export class RpcError extends Error {
  constructor(name, message = "", data = undefined) {
    super(message ? `${name}: ${message}`.slice(0, 300) : name);
    this.name = "RpcError";
    this.cause_name = name;
    this.data = data;
  }
}

export class NearRpc {
  /**
   * @param {string[]} urls endpoints, tried in order
   * @param {{archival?: string, fetch?: typeof fetch, timeoutMs?: number}} [opts]
   */
  constructor(urls, opts = {}) {
    this.urls = urls.filter(Boolean);
    if (!this.urls.length) throw new Error("no NEAR RPC endpoint configured");
    this.archival = opts.archival || "";
    this.fetch = opts.fetch ?? globalThis.fetch;
    this.timeoutMs = opts.timeoutMs ?? 20_000;
  }

  async #post(url, payload, timeoutMs) {
    let res;
    try {
      res = await this.fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (e) {
      throw new RpcError("RPC_UNREACHABLE", e?.name ?? String(e));
    }
    if (res.status === 429) throw new RpcError("RATE_LIMITED", `HTTP 429 from ${url}`);
    let body;
    try {
      body = await res.json();
    } catch {
      throw new RpcError("RPC_UNREACHABLE", `HTTP ${res.status}, not JSON`);
    }
    const err = body?.error;
    if (err && typeof err !== "object") {
      const text = String(err);
      if (RATE_LIMITED.test(text)) throw new RpcError("RATE_LIMITED", text);
      if (PLAN_LIMIT.test(text)) throw new RpcError("PROVIDER_LIMIT", text);
      throw new RpcError(res.status >= 500 ? "RPC_UNREACHABLE" : "RPC_ERROR", text.slice(0, 200));
    }
    if (err) {
      const name = String(err.cause?.name ?? err.name ?? "RPC_ERROR");
      const text = typeof err.data === "string" ? err.data : JSON.stringify(err.data ?? err.message ?? "");
      if (RATE_LIMITED.test(text) || RATE_LIMITED.test(name)) throw new RpcError("RATE_LIMITED", text);
      if (PLAN_LIMIT.test(text)) throw new RpcError("PROVIDER_LIMIT", text);
      throw new RpcError(name, text, err);
    }
    return body.result;
  }

  /** One JSON-RPC call; a rate-limited, unreachable or plan-limited endpoint hands it to the next. */
  async call(method, params, { urls = this.urls, timeoutMs = this.timeoutMs } = {}) {
    const payload = { jsonrpc: "2.0", id: "emporium-mcp", method, params };
    let last;
    for (let round = 0; round < 3; round++) {
      if (round) await new Promise((r) => setTimeout(r, 500 * 2 ** (round - 1)));
      for (const url of urls) {
        try {
          return await this.#post(url, payload, timeoutMs);
        } catch (e) {
          if (e instanceof RpcError && ["RATE_LIMITED", "RPC_UNREACHABLE", "PROVIDER_LIMIT"].includes(e.cause_name)) {
            last = e;
            continue;
          }
          throw e;
        }
      }
    }
    throw last ?? new RpcError("RPC_UNREACHABLE", "no endpoint answered");
  }

  /** A transaction's final outcome: asked without waiting first, then waited for, then from the archival node. */
  async txOutcome(tx, signer) {
    const params = (wait_until) => ({ tx_hash: tx, sender_account_id: signer, wait_until });
    try {
      const out = await this.call("tx", params("NONE"));
      if (out?.final_execution_status === "FINAL" || (out?.status && out?.final_execution_status === undefined)) return { outcome: out, source: "rpc" };
      return { outcome: await this.call("tx", params("FINAL"), { timeoutMs: 40_000 }), source: "rpc" };
    } catch (e) {
      if (!this.archival || this.urls.includes(this.archival)) throw e;
      return { outcome: await this.call("tx", params("FINAL"), { urls: [this.archival] }), source: "archival" };
    }
  }

  async blockTime(hash, archival = false) {
    for (const urls of archival && this.archival ? [[this.archival]] : [this.urls, ...(this.archival ? [[this.archival]] : [])]) {
      try {
        const b = await this.call("block", { block_id: hash }, { urls });
        return { height: b.header.height, time: new Date(Number(BigInt(b.header.timestamp) / 1_000_000n)).toISOString() };
      } catch {
        // an older block lives on the archival node only
      }
    }
    return null;
  }

  async view(contract, method, args) {
    const res = await this.call("query", {
      request_type: "call_function",
      finality: "final",
      account_id: contract,
      method_name: method,
      args_base64: Buffer.from(JSON.stringify(args)).toString("base64"),
    });
    return JSON.parse(Buffer.from(res.result).toString() || "null");
  }
}

const receipts = (outcome) => (outcome?.receipts_outcome ?? []).map((r) => r.outcome ?? {});

/** Every failed status in a final outcome (empty: it all ran). The trap: a delegate's own receipt can
 * succeed, and the transaction's status with it, while the token's receipt inside it fails. */
export function failures(outcome) {
  const out = [];
  if (outcome?.status?.Failure) out.push(outcome.status.Failure);
  const txStatus = outcome?.transaction_outcome?.outcome?.status;
  if (txStatus?.Failure) out.push(txStatus.Failure);
  for (const o of receipts(outcome)) if (o.status?.Failure) out.push(o.status.Failure);
  return out;
}

/** NEP-141 ft_transfer events the token contract logged in receipts that succeeded. */
export function ftTransfers(outcome, token) {
  const found = [];
  for (const o of receipts(outcome)) {
    if (o.executor_id !== token || o.status?.Failure) continue;
    for (const line of o.logs ?? []) {
      if (typeof line !== "string" || !line.startsWith("EVENT_JSON:")) continue;
      let ev;
      try {
        ev = JSON.parse(line.slice("EVENT_JSON:".length));
      } catch {
        continue;
      }
      if (ev?.standard === "nep141" && ev?.event === "ft_transfer") for (const d of ev.data ?? []) if (d && typeof d === "object") found.push(d);
    }
  }
  return found;
}

/**
 * Looks a payment up on chain. Returns what the chain says: whether every receipt ran, each USDC
 * transfer in it, and (when payTo is given) how much USDC reached the shop.
 */
export async function checkPayment(rpc, { tx, signer, usdc, payTo = "", payer = "" }) {
  const { outcome, source } = await rpc.txOutcome(tx, signer);
  const failed = failures(outcome);
  const transfers = ftTransfers(outcome, usdc).map((t) => ({ from: String(t.old_owner_id), to: String(t.new_owner_id), amount: String(t.amount), memo: t.memo }));
  let toShop = 0n;
  if (!failed.length) for (const t of transfers) if (t.to === payTo && (!payer || t.from === payer) && /^\d+$/.test(t.amount)) toShop += BigInt(t.amount);
  const block = outcome?.transaction_outcome?.block_hash ? await rpc.blockTime(outcome.transaction_outcome.block_hash, source === "archival") : null;
  return {
    tx,
    signer: outcome?.transaction?.signer_id ?? signer,
    receiver: outcome?.transaction?.receiver_id ?? "",
    final: outcome?.final_execution_status ?? "FINAL",
    succeeded: failed.length === 0,
    failures: failed,
    transfers,
    to_shop: toShop,
    block,
    source,
  };
}
