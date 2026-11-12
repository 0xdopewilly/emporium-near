// A transparent NEAR JSON-RPC forwarder on 127.0.0.1 that both implementations read the chain through.
//
// It changes nothing in what either side sees: each request body goes upstream as is and the upstream's
// answer comes back as is. It only (1) keeps TLS connections to the upstreams alive, so the comparison is
// not dominated by handshakes, (2) moves a request on to the next upstream (FastNEAR, then dRPC) or waits
// and retries when an upstream refuses it for its rate limit, and (3) counts calls. A transaction (send_tx)
// moves on only when it was refused for a rate limit (it never reached the chain); once an upstream has
// taken it, or the connection failed mid-way, it is never sent again here.
import http from "node:http";

const RATE_LIMITED = /rate.?limit|too many requests/i;
const PLAN_LIMIT = /free plan|free tier|upgrade to (?:a )?paid|quota exceeded/i;
const SENDS = new Set(["send_tx", "broadcast_tx_commit", "broadcast_tx_async"]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function startRpcProxy(upstreams, { rounds = 8 } = {}) {
  const stats = { calls: 0, byMethod: {}, byUpstream: {}, rateLimited: 0, retried: 0, failed: 0 };

  async function forward(body, method) {
    const send = SENDS.has(method);
    let last = "no upstream answered";
    let refusal = null; // the last upstream answer that refused the request, passed on as is if all refuse
    for (let round = 0; round < rounds; round++) {
      if (round) {
        stats.retried++;
        await sleep(Math.min(4000, 400 * 2 ** (round - 1)));
      }
      for (const url of upstreams) {
        let r, text;
        try {
          r = await fetch(url, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body,
            signal: AbortSignal.timeout(send ? 120_000 : 25_000),
          });
          text = await r.text();
        } catch (e) {
          last = `${e.name}: ${e.message} (${url})`;
          if (send) return { status: 502, text: JSON.stringify({ error: last }) }; // it may have landed
          continue;
        }
        const head = text.slice(0, 400);
        const limited = r.status === 429 || (RATE_LIMITED.test(head) && head.includes('"error"'));
        if (limited || (!send && ((r.status >= 500 && !text.startsWith("{")) || PLAN_LIMIT.test(head)))) {
          if (limited) stats.rateLimited++;
          last = `HTTP ${r.status} from ${url}`;
          refusal = { status: r.status, text };
          continue;
        }
        stats.byUpstream[url] = (stats.byUpstream[url] ?? 0) + 1;
        return { status: r.status, text };
      }
    }
    stats.failed++;
    return refusal ?? { status: 502, text: JSON.stringify({ error: last }) };
  }

  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = Buffer.concat(chunks).toString("utf8");
    let method = "?";
    try {
      method = JSON.parse(body).method ?? "?";
    } catch {}
    stats.calls++;
    stats.byMethod[method] = (stats.byMethod[method] ?? 0) + 1;
    const { status, text } = await forward(body, method);
    res.writeHead(status, { "content-type": "application/json" });
    res.end(text);
  });
  server.keepAliveTimeout = 30_000;
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${server.address().port}`;
  return { url, stats, close: () => new Promise((r) => server.close(r)) };
}
