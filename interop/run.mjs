#!/usr/bin/env node
// Emporium's pure-Python x402 `exact`-on-NEAR against the official TypeScript implementation (@x402/near),
// in both directions, on the same bytes, against real NEAR testnet state.
//
//   npm ci
//   PY=<python with x402 + Emporium's deps> node run.mjs [--n 120] [--m 120] [--settle] [--chain-truth]
//   PY=... node run.mjs --offline           # the same checks against tests/fake_near.py's in-memory chain
//
// Direction 1: Emporium's client (Python, x402ClientSync + ExactNearClient) signs; @x402/near's facilitator
//              (x402Facilitator + ExactNearScheme) verifies. Emporium's facilitator verifies the same payload.
// Direction 2: @x402/near's client (x402Client + ExactNearScheme) signs; Emporium's facilitator verifies,
//              and so does the official one.
// Tamper matrix: near-api-js builds each tampered payload once; both facilitators verify the same bytes.
// --settle: real settles, each way, confirmed on chain; then each settled payment is replayed at both.
// --chain-truth: the spec-silent edge cases are submitted to the chain unverified, to see who was right.
import { parseArgs } from "node:util";
import fs from "node:fs";
import path from "node:path";
import { execSync } from "node:child_process";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { x402Facilitator } from "@x402/core/facilitator";
import { x402Client } from "@x402/core/client";
import { ExactNearScheme as OfficialFacilitatorScheme } from "@x402/near/exact/facilitator";
import { ExactNearScheme as OfficialClientScheme } from "@x402/near/exact/client";
import { createFacilitatorNearSigner, createClientNearSigner } from "@x402/near";
import { startRpcProxy } from "./lib/rpc-proxy.mjs";
import { startBridge } from "./lib/bridge.mjs";
import { caseTools, catalogue } from "./lib/cases.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const USDC = "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af";
const NETWORK = "near:testnet";
const UPSTREAMS = ["https://test.rpc.fastnear.com", "https://near-testnet.drpc.org"];
const BLACKHOLE = "http://127.0.0.1:9"; // near:mainnet RPC: nothing here ever reaches mainnet

const { values: opt } = parseArgs({
  options: {
    offline: { type: "boolean", default: false },
    n: { type: "string", default: "100" },
    m: { type: "string", default: "100" },
    settle: { type: "boolean", default: false },
    settles: { type: "string", default: "1" },
    "chain-truth": { type: "boolean", default: false },
    concurrency: { type: "string", default: "4" },
    wallets: { type: "string", default: path.join(ROOT, ".dev", "near-wallets.json") },
    relayer: { type: "string", default: "interop-relay.emporium.testnet" },
    parent: { type: "string", default: "emporium.testnet" },
    python: { type: "string", default: process.env.PY || "python3" },
    out: { type: "string" },
    seed: { type: "string", default: "402" },
    only: { type: "string" }, // comma-separated case ids, for a quick look at a few tamper cases
    verbose: { type: "boolean", default: false },
  },
});
const N1 = Number(opt.n), N2 = Number(opt.m), CONC = Number(opt.concurrency);
const log = (...a) => console.log(...a);
const vlog = (...a) => opt.verbose && console.error(...a);

// ---- a seeded RNG, so a run's terms are reproducible ----
let rngState = Number(opt.seed) >>> 0;
const rnd = () => {
  rngState = (rngState + 0x6d2b79f5) >>> 0;
  let t = rngState;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const pick = (xs) => xs[Math.floor(rnd() * xs.length)];

async function pool(items, n, fn) {
  const out = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.max(1, n) }, async () => {
    while (next < items.length) {
      const k = next++;
      out[k] = await fn(items[k], k);
    }
  }));
  return out;
}

const version = (pkg) => JSON.parse(fs.readFileSync(path.join(HERE, "node_modules", pkg, "package.json"), "utf8")).version;
const sha = (s) => createHash("sha256").update(String(s)).digest("hex").slice(0, 16);

async function main() {
  const started = Date.now();
  const offline = opt.offline;
  const parent = opt.parent;
  const relayer = opt.relayer;
  let buyers, payTos, secrets = {}, rpcUrl, proxy = null;
  const bridge = startBridge(opt.python, { logStderr: opt.verbose });

  if (offline) {
    buyers = Array.from({ length: 5 }, (_, i) => `buyer-${i + 1}.${parent}`);
    payTos = [parent, `ada.${parent}`, `tunde.${parent}`];
    const s = await bridge.call("setup", { offline: true, relayer, buyers, pay_to: payTos });
    secrets = s.secrets;
    rpcUrl = s.rpc_url;
  } else {
    const wallets = JSON.parse(fs.readFileSync(opt.wallets, "utf8"));
    for (const [a, v] of Object.entries(wallets)) if (v && v.secret) secrets[a] = v.secret;
    if (!secrets[relayer]) throw new Error(`no key for the relayer ${relayer} in ${opt.wallets}`);
    if ([`ada-relay.${parent}`, `tunde-relay.${parent}`].includes(relayer)) throw new Error("the live shops' relayers are off limits");
    buyers = Object.entries(wallets).filter(([, v]) => v && v.role === "buyer").map(([a]) => a);
    payTos = [parent, `ada.${parent}`, `tunde.${parent}`, `ada-wallet.${parent}`];
    proxy = await startRpcProxy(UPSTREAMS);
    rpcUrl = proxy.url;
    await bridge.call("setup", { offline: false, relayer, wallets: opt.wallets, rpc_url: rpcUrl });
  }
  const rpcUrls = { "near:testnet": rpcUrl, "near:mainnet": BLACKHOLE };

  async function rpc(method, params) {
    const r = await fetch(rpcUrl, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: "interop", method, params }) });
    const j = await r.json();
    if (j.error) throw new Error(JSON.stringify(j.error).slice(0, 300));
    return j.result;
  }
  async function usdcOf(account) {
    const res = await rpc("query", { request_type: "call_function", finality: "final", account_id: USDC, method_name: "ft_balance_of", args_base64: Buffer.from(JSON.stringify({ account_id: account })).toString("base64") });
    return BigInt(JSON.parse(Buffer.from(res.result).toString()));
  }

  // ---- the official side ----
  const official = new x402Facilitator().register(["near:testnet", "near:mainnet"],
    new OfficialFacilitatorScheme(createFacilitatorNearSigner({ relayers: [{ accountId: relayer, secretKey: secrets[relayer] }], rpcUrls })));
  const officialClients = {};
  const officialClient = (account) => (officialClients[account] ??= new x402Client().register(NETWORK,
    new OfficialClientScheme(createClientNearSigner({ accountId: account, secretKey: secrets[account], rpcUrls }))));

  const norm = (r, t0) => ({ valid: !!r.isValid, reason: r.invalidReason ?? null, payer: r.payer ?? null, ms: Math.round(performance.now() - t0) });
  async function officialVerify(paymentPayload, paymentRequirements) {
    const t0 = performance.now();
    try {
      return norm(await official.verify(paymentPayload, paymentRequirements), t0);
    } catch (e) {
      return { valid: false, reason: "sdk_error", message: String(e.message).slice(0, 200), payer: null, ms: Math.round(performance.now() - t0) };
    }
  }
  async function emporiumVerify(paymentPayload, paymentRequirements) {
    const t0 = performance.now();
    const r = await bridge.call("verify", { paymentPayload, paymentRequirements });
    return { ...norm(r, t0), ...(r.invalidMessage && !r.isValid ? { message: String(r.invalidMessage).slice(0, 200) } : {}) };
  }
  const both = async (p, r) => {
    const [o, e] = await Promise.all([officialVerify(p, r), emporiumVerify(p, r)]);
    return { official: o, emporium: e, agree: o.valid === e.valid };
  };

  // ---- who can pay how much ----
  const balances = {};
  for (const b of buyers) balances[b] = await usdcOf(b);
  const payers = buyers.filter((b) => balances[b] >= 200_000n);
  if (payers.length < 2) throw new Error("need at least two buyers holding 0.2 test USDC");
  const cap = payers.reduce((m, b) => (balances[b] < m ? balances[b] : m), balances[payers[0]]);
  const AMOUNTS = [1n, 7n, 100n, 999n, 10_000n, 12_345n, 50_000n, 99_999n, 150_000n].filter((a) => a <= cap);
  const TIMEOUTS = [60, 120, 300, 600, 1800, 3600, 86400];
  const terms = (k, signerLabel) => {
    const payer = payers[k % payers.length];
    const payTo = pick([...payTos, ...payers.filter((b) => b !== payer)]);
    const req = { scheme: "exact", network: NETWORK, amount: String(pick(AMOUNTS)), asset: USDC, payTo, maxTimeoutSeconds: pick(TIMEOUTS), extra: {} };
    const required = { x402Version: 2, resource: { url: `https://interop.example/${signerLabel}/${k}`, description: "interop check", mimeType: "application/json" }, accepts: [req] };
    return { payer, req, required };
  };
  log(`${offline ? "offline (fake chain)" : "NEAR testnet"}: ${payers.length} payers, ${payTos.length} shops; relayer ${relayer}`);

  // ---- direction 1: Emporium signs, the official facilitator verifies ----
  const t1 = Date.now();
  const dir1 = await pool(Array.from({ length: N1 }, (_, k) => terms(k, "emporium")), CONC, async ({ payer, req, required }, k) => {
    try {
      const { paymentPayload } = await bridge.call("sign", { account: payer, paymentRequired: required });
      const v = await both(paymentPayload, req);
      vlog("d1", k, v.official.valid, v.official.reason, v.emporium.valid, v.emporium.reason);
      return { k, payer, payTo: req.payTo, amount: req.amount, maxTimeoutSeconds: req.maxTimeoutSeconds, sda: sha(paymentPayload.payload.signedDelegateAction), ...v };
    } catch (e) {
      return { k, payer, error: String(e.message).slice(0, 300) };
    }
  });
  const d1s = summarize(dir1, Date.now() - t1);
  log(`direction 1: ${d1s.acceptedByOfficial}/${N1} Emporium-signed payloads accepted by @x402/near ${version("@x402/near")} (Emporium's own facilitator: ${d1s.acceptedByEmporium}/${N1})`);

  // ---- direction 2: the official client signs, Emporium's facilitator verifies ----
  const t2 = Date.now();
  const dir2 = await pool(Array.from({ length: N2 }, (_, k) => terms(k, "official")), CONC, async ({ payer, req, required }, k) => {
    try {
      const paymentPayload = await officialClient(payer).createPaymentPayload(required);
      const v = await both(paymentPayload, req);
      vlog("d2", k, v.official.valid, v.official.reason, v.emporium.valid, v.emporium.reason);
      return { k, payer, payTo: req.payTo, amount: req.amount, maxTimeoutSeconds: req.maxTimeoutSeconds, sda: sha(paymentPayload.payload.signedDelegateAction), ...v };
    } catch (e) {
      return { k, payer, error: String(e.message).slice(0, 300) };
    }
  });
  const d2s = summarize(dir2, Date.now() - t2);
  log(`direction 2: ${d2s.acceptedByEmporium}/${N2} @x402/near-signed payloads accepted by Emporium (the official facilitator: ${d2s.acceptedByOfficial}/${N2})`);

  // ---- the tamper matrix ----
  const [buyer, other] = payers;
  const ctx = {
    rpc, network: NETWORK, usdc: USDC, buyer, buyerSecret: secrets[buyer], other, otherSecret: secrets[other],
    relayer, relayerSecret: secrets[relayer], payTo: parent, amount: "10000",
    unregistered: `no-usdc-storage.${parent}`, notContract: other, missingAccount: `no-such-account-x402.${parent}`,
  };
  const tools = caseTools(ctx);
  let cases = catalogue(ctx);
  if (opt.only) cases = cases.filter((c) => opt.only.split(",").includes(c.id));
  const matrix = await pool(cases, Math.min(CONC, 3), async (c) => {
    try {
      const { paymentPayload, paymentRequirements } = await tools.pay(c.o);
      const v = await both(paymentPayload, paymentRequirements);
      const decoded = await bridge.call("decode", { signedDelegateAction: String(paymentPayload.payload?.signedDelegateAction ?? "") });
      const row = { id: c.id, cls: c.cls, title: c.title, expect: c.expect, ...v, emporiumDecodes: decoded.ok };
      row.pass = c.expect === "accept" ? v.official.valid && v.emporium.valid : c.expect === "reject" ? !v.official.valid && !v.emporium.valid : null;
      vlog(c.id, c.expect, v.official.valid, v.official.reason, v.emporium.valid, v.emporium.reason);
      return row;
    } catch (e) {
      return { id: c.id, cls: c.cls, title: c.title, expect: c.expect, error: String(e.stack || e.message).slice(0, 400) };
    }
  });

  // ---- real settles, each way, then replays ----
  const settles = [];
  const replays = [];
  if (opt.settle) {
    const SETTLE_AMOUNT = "10000"; // 0.01 test USDC
    const rounds = Number(opt.settles);
    for (let i = 0; i < rounds; i++) {
      for (const way of ["emporium-signed -> official settles", "official-signed -> emporium settles"]) {
        const payer = payers[(2 + settles.length) % payers.length];
        const req = { scheme: "exact", network: NETWORK, amount: SETTLE_AMOUNT, asset: USDC, payTo: parent, maxTimeoutSeconds: 600, extra: {} };
        const required = { x402Version: 2, resource: { url: `https://interop.example/settle/${settles.length}`, description: "interop settle", mimeType: "application/json" }, accepts: [req] };
        const rec = { way, payer, payTo: parent, amount: SETTLE_AMOUNT };
        try {
          const emporiumSigns = way.startsWith("emporium");
          const paymentPayload = emporiumSigns
            ? (await bridge.call("sign", { account: payer, paymentRequired: required })).paymentPayload
            : await officialClient(payer).createPaymentPayload(required);
          rec.before = (await usdcOf(payer)).toString();
          rec.verify = await both(paymentPayload, req);
          const t0 = performance.now();
          const s = emporiumSigns ? await official.settle(paymentPayload, req) : await bridge.call("settle", { paymentPayload, paymentRequirements: req });
          rec.settleMs = Math.round(performance.now() - t0);
          rec.settle = { success: !!s.success, transaction: s.transaction || "", errorReason: s.errorReason ?? null, errorMessage: s.errorMessage ?? null };
          if (s.transaction) {
            rec.chain = await bridge.call("outcome", { tx: s.transaction, sender: relayer, token: USDC, payer, payTo: parent });
            rec.confirmed = rec.chain.final && rec.chain.paid === SETTLE_AMOUNT && rec.chain.failures.length === 0;
            rec.explorer = `https://testnet.nearblocks.io/txns/${s.transaction}`;
          }
          rec.after = (await usdcOf(payer)).toString();
          if (rec.confirmed) {
            // the same signed payment again, at both facilitators (verify, and settle at the other one): it
            // must not pay twice
            const again = await both(paymentPayload, req);
            replays.push({ id: `R${replays.length + 1}`, cls: "stale nonce", title: `replay of a payment already settled (${way})`, expect: "reject", ...again, pass: !again.official.valid && !again.emporium.valid });
            const otherSettle = emporiumSigns ? await bridge.call("settle", { paymentPayload, paymentRequirements: req }) : await official.settle(paymentPayload, req);
            rec.crossReplaySettle = { by: emporiumSigns ? "emporium" : "official", success: !!otherSettle.success, errorReason: otherSettle.errorReason ?? null };
            rec.afterReplay = (await usdcOf(payer)).toString();
          }
        } catch (e) {
          rec.error = String(e.stack || e.message).slice(0, 400);
        }
        settles.push(rec);
        log(`settle ${way}: ${rec.settle?.success ? "ok" : "FAILED"} ${rec.settle?.transaction || rec.error || rec.settle?.errorReason || ""}`);
      }
    }
  }

  // ---- the chain's verdict on the edge cases ----
  if (opt["chain-truth"] && !offline) {
    for (const row of matrix.filter((r) => r.expect === "edge")) {
      const c = cases.find((x) => x.id === row.id);
      if (!c.truth) continue;
      try {
        const { paymentPayload, paymentRequirements } = await tools.pay(c.o); // fresh head and nonce
        const sda = paymentPayload.payload.signedDelegateAction;
        const decoded = await bridge.call("decode", { signedDelegateAction: sda });
        let truth;
        if (decoded.ok) {
          truth = { via: "relayer, unverified", ...(await bridge.call("relay_unchecked", { signedDelegateAction: sda, token: USDC, payer: buyer, payTo: parent, amount: paymentRequirements.amount })) };
        } else {
          const s = await official.settle(paymentPayload, paymentRequirements);
          truth = { via: "@x402/near settle", submitted: !!s.transaction, tx: s.transaction, success: !!s.success, errorReason: s.errorReason ?? null, errorMessage: s.errorMessage ?? null };
          if (s.transaction) Object.assign(truth, await bridge.call("outcome", { tx: s.transaction, sender: relayer, token: USDC, payer: buyer, payTo: parent }));
        }
        truth.paysAsAsked = truth.submitted ? (truth.paid === paymentRequirements.amount && (truth.failures ?? []).length === 0 && BigInt(paymentRequirements.amount) > 0n) : false;
        if (truth.tx) truth.explorer = `https://testnet.nearblocks.io/txns/${truth.tx}`;
        row.chain = truth;
        log(`chain truth ${row.id}: ${truth.paysAsAsked ? "pays as asked" : "does NOT pay"} ${truth.tx || truth.error || ""}`);
      } catch (e) {
        row.chain = { error: String(e.message).slice(0, 300) };
      }
    }
  }

  // ---- results ----
  const rows = [...matrix, ...replays];
  const judged = rows.filter((r) => r.expect !== "edge" && !r.error);
  const tampered = judged.filter((r) => r.expect === "reject");
  const controls = judged.filter((r) => r.expect === "accept");
  const edges = rows.filter((r) => r.expect === "edge");
  const sum = {
    tampered: tampered.length,
    bothReject: tampered.filter((r) => !r.official.valid && !r.emporium.valid).length,
    controls: controls.length,
    bothAccept: controls.filter((r) => r.official.valid && r.emporium.valid).length,
    agree: judged.filter((r) => r.agree).length,
    judged: judged.length,
    errors: rows.filter((r) => r.error).map((r) => ({ id: r.id, error: r.error })),
    classes: [...new Set(tampered.map((r) => r.cls))],
    // spec: a payer is named only once verified; never from the payload's unverified sender_id
    emporiumNamesPayerOfficialDoesNot: judged.filter((r) => !r.emporium.valid && r.emporium.payer && !r.official.payer).map((r) => r.id),
    officialNamesPayerEmporiumDoesNot: judged.filter((r) => !r.official.valid && r.official.payer && !r.emporium.payer).map((r) => r.id),
    edges: edges.map((r) => {
      const pays = r.chain?.paysAsAsked ?? null;
      const right = (v) => (pays === null ? null : v === pays);
      return { id: r.id, title: r.title, official: r.official?.valid, emporium: r.emporium?.valid, agree: r.agree,
        chainPaysAsAsked: pays, officialMatchesChain: right(r.official?.valid), emporiumMatchesChain: right(r.emporium?.valid),
        tx: r.chain?.tx ?? null, chainError: r.chain?.error ?? null };
    }),
  };
  log(`tamper matrix: ${sum.tampered} tampered payloads in ${sum.classes.length} classes, both reject ${sum.bothReject}/${sum.tampered}; ${sum.controls} controls, both accept ${sum.bothAccept}/${sum.controls}; agreement ${sum.agree}/${sum.judged}${sum.errors.length ? `; ${sum.errors.length} cases errored` : ""}`);
  for (const e of sum.edges) log(`  edge ${e.id} ${e.title}: @x402/near ${e.official ? "accepts" : "rejects"}, Emporium ${e.emporium ? "accepts" : "rejects"}${e.chainPaysAsAsked === null ? "" : `; on chain it ${e.chainPaysAsAsked ? "pays as asked" : "does not pay"}`}`);
  const confirmed = settles.filter((s) => s.confirmed);
  if (opt.settle) log(`real settles: ${confirmed.length}/${settles.length} confirmed on ${offline ? "the fake chain" : "NEAR testnet"}`);

  let commit = null;
  try { commit = execSync("git rev-parse HEAD", { cwd: ROOT }).toString().trim(); } catch {}
  const pyv = await bridge.call("versions");
  const result = {
    generatedAt: new Date().toISOString(),
    mode: offline ? "offline (tests/fake_near.py)" : "NEAR testnet",
    network: NETWORK,
    rpc: offline ? { url: "fake chain" } : { upstreams: UPSTREAMS, ...proxy.stats },
    versions: { "@x402/near": version("@x402/near"), "@x402/core": version("@x402/core"), "near-api-js": version("near-api-js"), node: process.version, x402Python: pyv.x402, python: pyv.python, emporiumCommit: commit },
    accounts: { relayer, payers, payTos, tamperBuyer: buyer },
    seed: Number(opt.seed),
    direction1: { what: "Emporium's client signs; @x402/near's facilitator verifies (Emporium's too)", n: N1, ...d1s, cases: dir1 },
    direction2: { what: "@x402/near's client signs; Emporium's facilitator verifies (the official too)", n: N2, ...d2s, cases: dir2 },
    tamper: { summary: sum, cases: rows },
    settles,
    seconds: Math.round((Date.now() - started) / 1000),
  };
  const out = opt.out ?? path.join(ROOT, "bench", "results", offline ? "interop-official-offline.json" : "interop-official.json");
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(result, null, 1));
  log(`wrote ${path.relative(process.cwd(), out)} (${result.seconds} s${proxy ? `, ${proxy.stats.calls} RPC calls, ${proxy.stats.rateLimited} rate-limited` : ""})`);

  await bridge.close();
  if (proxy) await proxy.close();
  const ok = d1s.acceptedByOfficial === N1 && d2s.acceptedByEmporium === N2 && sum.agree === sum.judged && sum.bothReject === sum.tampered &&
    sum.bothAccept === sum.controls && sum.errors.length === 0 && (!opt.settle || confirmed.length === settles.length);
  process.exit(ok ? 0 : 1);
}

function summarize(rows, ms) {
  const ok = rows.filter((r) => !r.error);
  const median = (xs) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : null; };
  return {
    acceptedByOfficial: ok.filter((r) => r.official.valid).length,
    acceptedByEmporium: ok.filter((r) => r.emporium.valid).length,
    agree: ok.filter((r) => r.agree).length,
    errors: rows.filter((r) => r.error).length,
    rejections: ok.filter((r) => !r.official.valid || !r.emporium.valid).map((r) => ({ k: r.k, official: r.official.reason, emporium: r.emporium.reason })),
    medianVerifyMs: { official: median(ok.map((r) => r.official.ms)), emporium: median(ok.map((r) => r.emporium.ms)) },
    seconds: Math.round(ms / 1000),
  };
}

main().catch((e) => {
  console.error(e.stack || e);
  process.exit(2);
});
