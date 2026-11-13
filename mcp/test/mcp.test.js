// emporium-mcp's tools end to end through an MCP client, against fakes: a fake fetch for the shop and NEAR
// RPC in keyless mode, and local HTTP servers (a shop that checks the signed delegate, and a NEAR RPC) for
// paying with your own account through the official @x402/near client. No network.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer as httpServer } from "node:http";
import { createHash, createPrivateKey, createPublicKey } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { encodePaymentRequiredHeader, decodePaymentSignatureHeader, encodePaymentResponseHeader } from "@x402/core/http";
import { decodeSignedDelegateB64, parseFtTransferArgs } from "@x402/near";
import { createServer } from "../src/server.js";
import { configFromEnv, ConfigError, toAtomic, usd } from "../src/config.js";
import { sha256 } from "../src/shop.js";

const SHOP = "https://shop.test";
const RPC = "https://rpc.test";
const RPC2 = "https://rpc2.test";
const ARCHIVE = "https://archive.test";
const USDC = "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af";
const TX = "8ch29cf6oLqz5XDDbeKeMoCBfTj1wiLfggaG8b2QhWg4";
// A throwaway key for the fake chain only, derived here from a fixed seed (it holds nothing anywhere).
const { KEY, PUBLIC_KEY } = seededKey("emporium-mcp-test-buyer");

function seededKey(text) {
  const seed = createHash("sha256").update(text).digest();
  const pkcs8 = Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), seed]);
  const pub = createPublicKey(createPrivateKey({ key: pkcs8, format: "der", type: "pkcs8" })).export({ format: "der", type: "spki" }).subarray(-32);
  return { KEY: `ed25519:${b58(Buffer.concat([seed, pub]))}`, PUBLIC_KEY: `ed25519:${b58(pub)}` };
}

function b58(bytes) {
  const ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  let n = BigInt("0x" + (Buffer.from(bytes).toString("hex") || "0"));
  let out = "";
  while (n > 0n) {
    out = ALPHABET[Number(n % 58n)] + out;
    n /= 58n;
  }
  for (const b of bytes) {
    if (b !== 0) break;
    out = "1" + out;
  }
  return out;
}

const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

function cat(over = {}) {
  return {
    emporium: "1",
    name: "Ada's Emporium",
    tagline: "Skills for hire.",
    open: true,
    pay_to: "ada.emporium.testnet",
    network: "near:testnet",
    network_label: "NEAR testnet",
    asset_contract: USDC,
    charged: "only on delivery",
    relayer: "ada-relay.emporium.testnet",
    services: [
      { id: "code-review", title: "Code review", description: "Bugs first.", price: "$0.25", price_usd: "0.25", example: "def add(a, b):\n    return a - b", timeout_seconds: 180, input: { properties: { input: { maxLength: 8000 } } }, subcontracts: [] },
      { id: "launch-post", title: "Launch post", description: "English and Yoruba.", price: "$0.30", price_usd: "0.30", example: "x", timeout_seconds: 180, subcontracts: [{ shop: "http://127.0.0.1:9503", service: "translate-yoruba" }] },
    ],
    demo: { enabled: true, endpoint: `${SHOP}/demo/hire`, daily_left: 29, daily_jobs: 30, per_address: "3 per 10 minutes", max_price: "$0.50", payer: "demo-buyer.emporium.testnet", input: { input: "up to 2000 characters" } },
    ...over,
  };
}

function outcome({ amount = "250000", from = "demo-buyer.emporium.testnet", to = "ada.emporium.testnet", tokenFails = false } = {}) {
  const event = `EVENT_JSON:${JSON.stringify({ standard: "nep141", version: "1.0.0", event: "ft_transfer", data: [{ old_owner_id: from, new_owner_id: to, amount }] })}`;
  return {
    final_execution_status: "FINAL",
    status: { SuccessValue: "" },
    transaction: { hash: TX, signer_id: "ada-relay.emporium.testnet", receiver_id: from },
    transaction_outcome: { id: TX, block_hash: "BlockHash111", outcome: { executor_id: "ada-relay.emporium.testnet", status: { SuccessReceiptId: "r1" }, logs: [] } },
    receipts_outcome: [
      { id: "r1", outcome: { executor_id: from, status: { SuccessValue: "" }, logs: [] } },
      { id: "r2", outcome: { executor_id: USDC, status: tokenFails ? { Failure: { ActionError: { kind: { FunctionCallError: { ExecutionError: "Smart contract panicked: The account doesn't have enough balance" } } } } } : { SuccessValue: "" }, logs: tokenFails ? [] : [event] } },
    ],
  };
}

/** A fetch that answers from `route` and records every call. */
function fakeFetch(route) {
  const calls = [];
  const fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url: url.toString(), method: init.method ?? "GET", body });
    const res = await route(url, body);
    if (!res) throw new TypeError(`fetch failed: unexpected ${url}`);
    return res;
  };
  return { fetch, calls };
}

const rpcResult = (result) => json({ jsonrpc: "2.0", id: "x", result });
const rpcError = (cause, data = cause) => json({ jsonrpc: "2.0", id: "x", error: { name: "HANDLER_ERROR", cause: { name: cause }, data } });

function config(env = {}) {
  return configFromEnv({ EMPORIUM_SHOP_URL: SHOP, EMPORIUM_NEAR_RPC: `${RPC},${RPC2}`, EMPORIUM_NEAR_ARCHIVAL_RPC: ARCHIVE, ...env });
}

async function connect(cfg, fetch) {
  const [serverSide, clientSide] = InMemoryTransport.createLinkedPair();
  await createServer(cfg, { fetch }).connect(serverSide);
  const client = new Client({ name: "test", version: "0" });
  await client.connect(clientSide);
  return client;
}

const textOf = (r) => r.content.map((c) => c.text).join("\n");

test("three tools: look, hire, verify; only hire spends", async () => {
  const client = await connect(config(), fakeFetch(() => undefined).fetch);
  const { tools } = await client.listTools();
  assert.deepEqual(tools.map((t) => t.name), ["emporium_shop", "emporium_hire", "emporium_verify"]);
  const by = Object.fromEntries(tools.map((t) => [t.name, t]));
  assert.deepEqual(by.emporium_hire.inputSchema.required, ["service", "input"]);
  assert.match(by.emporium_hire.description, /keyless demo/);
  assert.match(by.emporium_hire.description, /real NEAR testnet USDC payment/);
  assert.equal(by.emporium_hire.annotations.readOnlyHint, false);
  assert.equal(by.emporium_shop.annotations.readOnlyHint, true);
  assert.equal(by.emporium_verify.annotations.readOnlyHint, true);
  assert.deepEqual(by.emporium_verify.inputSchema.required, ["tx"]);
});

test("the shop tool reads the menu, the demo's allowance and the shop's own numbers", async () => {
  const { fetch } = fakeFetch((url) => {
    if (url.pathname === "/.well-known/emporium.json") return json(cat());
    if (url.pathname === "/feed") return json({ jobs_settled: 73, earned_total: "$20.35", p50_seconds: 27.2, not_charged: 4, jobs: [] });
  });
  const client = await connect(config(), fetch);
  const res = await client.callTool({ name: "emporium_shop", arguments: {} });
  const out = textOf(res);
  assert.match(out, /^Ada's Emporium \(https:\/\/shop\.test\)/);
  assert.match(out, /- code-review \(\$0\.25\): Code review\. Bugs first\. Example input: "def add/);
  assert.match(out, /- launch-post \(\$0\.30\).*It hires translate-yoruba from another shop/);
  assert.match(out, /Keyless demo: on\. 29 of 30 demo jobs left today \(UTC\)/);
  assert.match(out, /73 jobs delivered, \$20\.35 earned, median delivery 27\.2 s/);
  assert.equal(res.structuredContent.demo.daily_left, 29);
  assert.equal(res.structuredContent.pays_with, "keyless demo");
});

test("a keyless hire: the demo endpoint gets the service and input only; the receipt is checked on chain", async () => {
  const receipt = { amount: "$0.25", asset: "USDC", network: "near:testnet", payer: "demo-buyer.emporium.testnet", pay_to: "ada.emporium.testnet", tx: TX, explorer: `https://testnet.nearblocks.io/txns/${TX}`, shop: "Ada's Emporium", output_sha256: sha256("Bug: add subtracts."), asset_contract: USDC, settled_by: "ada-relay.emporium.testnet", paid: "$0.25", seconds: 21.3 };
  let rpcCalls = 0;
  const { fetch, calls } = fakeFetch((url, body) => {
    if (url.pathname === "/.well-known/emporium.json") return json(cat());
    if (url.pathname === "/demo/hire") return json({ shop: "Ada's Emporium", service: "code-review", output: "Bug: add subtracts.", receipt, demo: { payer: "demo-buyer.emporium.testnet", daily_left: 28 } });
    if (url.origin === RPC) {
      rpcCalls++;
      if (body.method === "tx") return rpcResult(outcome());
      if (body.method === "block") return rpcResult({ header: { height: 277428003, timestamp: "1794351686954000000" } });
    }
  });
  const client = await connect(config(), fetch);
  const res = await client.callTool({ name: "emporium_hire", arguments: { service: "code-review", input: "def add(a, b):\n    return a - b" } });
  const out = textOf(res);
  assert.equal(res.isError, undefined, out);
  assert.match(out, /^Bug: add subtracts\.\n\n---\n/);
  assert.match(out, /Receipt: paid \$0\.25 in USDC on NEAR testnet, from demo-buyer\.emporium\.testnet \(the shop's keyless demo account\) to ada\.emporium\.testnet, settled after delivery by the shop's relayer ada-relay\.emporium\.testnet/);
  assert.ok(out.includes(`Transaction: ${TX}  https://testnet.nearblocks.io/txns/${TX}`));
  assert.match(out, /SHA-256 of the deliverable: [0-9a-f]{64} \(matches the text above\)/);
  assert.match(out, /On chain: confirmed\. \$0\.25 in USDC reached ada\.emporium\.testnet in block 277428003/);
  assert.match(out, /Keyless demo jobs left today: 28\./);
  const demoCall = calls.find((c) => c.url.endsWith("/demo/hire"));
  assert.deepEqual(demoCall.body, { service: "code-review", input: "def add(a, b):\n    return a - b" }, "the demo is told the service and the input, nothing else");
  assert.equal(res.structuredContent.onchain.confirmed, true);
  assert.equal(res.structuredContent.paid_with, "keyless demo");
  assert.ok(rpcCalls >= 1);
});

test("keyless refusals say what to do: demo off, rate-limited, a mainnet shop", async () => {
  let shopCat = cat({ demo: undefined });
  let demoAnswer = json({ error: "3 demo jobs per 10 minutes from one address; try again in 7 min" }, 429, { "Retry-After": "420" });
  const { fetch, calls } = fakeFetch((url) => {
    if (url.pathname === "/.well-known/emporium.json") return json(shopCat);
    if (url.pathname === "/demo/hire") return demoAnswer;
  });
  const client = await connect(config(), fetch);
  let res = await client.callTool({ name: "emporium_hire", arguments: { service: "code-review", input: "x" } });
  assert.equal(res.isError, true);
  assert.match(textOf(res), /has no keyless demo[\s\S]*Set EMPORIUM_NEAR_ACCOUNT and EMPORIUM_NEAR_KEY/);
  assert.equal(calls.filter((c) => c.url.endsWith("/demo/hire")).length, 0, "nothing is posted to a shop without a demo");

  shopCat = cat();
  res = await client.callTool({ name: "emporium_hire", arguments: { service: "code-review", input: "x" } });
  assert.equal(res.isError, true);
  assert.match(textOf(res), /3 demo jobs per 10 minutes[\s\S]*rate-limited\. To hire without waiting/);
  assert.equal(res.structuredContent.http_status, 429);

  res = await client.callTool({ name: "emporium_hire", arguments: { service: "nope", input: "x" } });
  assert.match(textOf(res), /has no service "nope"; it sells: code-review, launch-post/);

  shopCat = cat({ network: "near:mainnet" });
  res = await client.callTool({ name: "emporium_hire", arguments: { service: "code-review", input: "x" } });
  assert.match(textOf(res), /on NEAR mainnet, where there is no keyless demo/);
});

test("verify: a payment the chain confirms, one whose token receipt failed, and the endpoints it falls back to", async () => {
  let answer = () => rpcResult(outcome());
  const { fetch, calls } = fakeFetch((url, body) => {
    if (url.pathname === "/.well-known/emporium.json") return json(cat());
    if (url.origin === RPC) return json({ error: "rate limit exceeded" }, 429);
    if (url.origin === RPC2 || url.origin === ARCHIVE) {
      if (body.method === "tx") return answer(url);
      if (body.method === "block") return rpcResult({ header: { height: 5, timestamp: "1794351686954000000" } });
    }
  });
  const client = await connect(config(), fetch);
  let res = await client.callTool({ name: "emporium_verify", arguments: { tx: TX } });
  let out = textOf(res);
  assert.match(out, /^Confirmed on NEAR testnet: \$0\.25 in USDC reached ada\.emporium\.testnet \(Ada's Emporium\)\./);
  assert.match(out, /signed by ada-relay\.emporium\.testnet/);
  assert.match(out, /0\.25 USDC from demo-buyer\.emporium\.testnet to ada\.emporium\.testnet/);
  const txCall = calls.find((c) => c.url.startsWith(RPC2) && c.body.method === "tx");
  assert.deepEqual(txCall.body.params, { tx_hash: TX, sender_account_id: "ada-relay.emporium.testnet", wait_until: "NONE" }, "the signer defaults to the shop's relayer");

  answer = () => rpcResult(outcome({ tokenFails: true }));
  res = await client.callTool({ name: "emporium_verify", arguments: { tx: TX, account: "ada-relay.emporium.testnet" } });
  out = textOf(res);
  assert.match(out, /^NOT a payment: a receipt of this transaction failed/);
  assert.equal(res.structuredContent.confirmed, false);

  // a transaction the public nodes no longer keep is read from the archival node
  answer = (url) => (url.origin === ARCHIVE ? rpcResult(outcome()) : rpcError("UNKNOWN_TRANSACTION"));
  res = await client.callTool({ name: "emporium_verify", arguments: { tx: TX } });
  assert.match(textOf(res), /^Confirmed on NEAR testnet[\s\S]*\(read from an archival node\)/);

  answer = () => rpcError("UNKNOWN_TRANSACTION");
  const noArchive = await connect(config({ EMPORIUM_NEAR_ARCHIVAL_RPC: "" }), fetch);
  res = await noArchive.callTool({ name: "emporium_verify", arguments: { tx: TX } });
  assert.equal(res.isError, true);
  assert.match(textOf(res), /has no transaction .* signed by ada-relay\.emporium\.testnet/);
});

// ---- paying from your own account: local HTTP servers for the shop and for NEAR RPC ----------------
function listen(handler) {
  return new Promise((resolve) => {
    const srv = httpServer(handler).listen(0, "127.0.0.1", () => resolve({ srv, url: `http://127.0.0.1:${srv.address().port}` }));
  });
}

async function readBody(req) {
  let b = "";
  for await (const c of req) b += c;
  return b;
}

async function fakeChain() {
  const state = { height: 277_000_000, nonce: 276_999_999_000_000n, txs: [] };
  const { srv, url } = await listen(async (req, res) => {
    const { method, params, id } = JSON.parse(await readBody(req));
    let result;
    if (method === "block") result = { header: { height: state.height, hash: "11111111111111111111111111111111", timestamp: "1794351686954000000" } };
    else if (method === "query" && params.request_type === "view_access_key") {
      result = params.public_key === PUBLIC_KEY ? { nonce: Number(state.nonce), permission: "FullAccess", block_height: state.height, block_hash: "11111111111111111111111111111111" } : undefined;
    } else if (method === "tx") result = outcome({ from: "mybuyer.testnet", amount: "250000" });
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify(result === undefined ? { jsonrpc: "2.0", id, error: { name: "HANDLER_ERROR", cause: { name: "UNKNOWN_ACCESS_KEY" }, data: "no such key" } } : { jsonrpc: "2.0", id, result }));
  });
  return { srv, url, state };
}

/** A shop that answers 402 and, given a payment, checks the delegate the way a facilitator would. */
async function fakeShop({ payTo = "ada.emporium.testnet", askPayTo = payTo, network = "near:testnet" } = {}) {
  const seen = { paid: [], delegates: [] };
  let base;
  const { srv, url } = await listen(async (req, res) => {
    const send = (status, body, headers = {}) => {
      res.writeHead(status, { "Content-Type": "application/json", ...headers });
      res.end(JSON.stringify(body));
    };
    if (req.method === "GET" && req.url === "/.well-known/emporium.json") return send(200, cat({ pay_to: payTo, network, demo: undefined }));
    if (req.method === "POST" && req.url === "/jobs/code-review") {
      const { input } = JSON.parse(await readBody(req));
      const required = { scheme: "exact", network, amount: "250000", asset: USDC, payTo: askPayTo, maxTimeoutSeconds: 600, extra: {} };
      const sig = req.headers["payment-signature"];
      if (!sig) {
        const pr = { x402Version: 2, error: "Payment required", resource: { url: `${base}/jobs/code-review`, description: "Code review", mimeType: "application/json" }, accepts: [required] };
        return send(402, {}, { "PAYMENT-REQUIRED": encodePaymentRequiredHeader(pr) });
      }
      const payload = decodePaymentSignatureHeader(sig);
      const d = decodeSignedDelegateB64(payload.payload.signedDelegateAction);
      seen.paid.push(payload);
      seen.delegates.push({ ...d.delegate, signatureOk: d.verifySignature(), args: parseFtTransferArgs(d.delegate.functionCall.args) });
      const out = `Review of ${input.length} chars: add() subtracts.`;
      const receipt = { amount: "$0.25", asset: "USDC", network, payer: d.delegate.senderId, pay_to: payTo, tx: TX, shop: "Ada's Emporium", output_sha256: sha256(out), asset_contract: USDC, settled_by: "ada-relay.emporium.testnet" };
      return send(200, { job: "job_1", service: "code-review", output: out, receipt }, { "PAYMENT-RESPONSE": encodePaymentResponseHeader({ success: true, transaction: TX, network, payer: d.delegate.senderId }) });
    }
    send(404, { error: "not found" });
  });
  base = url;
  return { srv, url, seen };
}

test("paying from your own account: the official @x402/near client signs exactly the listed price to the shop", async () => {
  const chain = await fakeChain();
  const shop = await fakeShop();
  try {
    const cfg = configFromEnv({ EMPORIUM_SHOP_URL: shop.url, EMPORIUM_NEAR_ACCOUNT: "mybuyer.testnet", EMPORIUM_NEAR_KEY: KEY, EMPORIUM_NEAR_RPC: chain.url, EMPORIUM_NEAR_ARCHIVAL_RPC: "" });
    const client = await connect(cfg, globalThis.fetch);
    const { tools } = await client.listTools();
    assert.match(tools.find((t) => t.name === "emporium_hire").description, /from your NEAR account mybuyer\.testnet on near:testnet, over x402 `exact` on NEAR with the official @x402\/near client/);
    const res = await client.callTool({ name: "emporium_hire", arguments: { service: "code-review", input: "def add(a, b): return a - b" } });
    const out = textOf(res);
    assert.equal(res.isError, undefined, out);
    const [d] = shop.seen.delegates;
    assert.equal(d.signatureOk, true);
    assert.deepEqual([d.senderId, d.receiverId, d.functionCall.methodName, d.args.receiver_id, d.args.amount, d.functionCall.deposit], ["mybuyer.testnet", USDC, "ft_transfer", "ada.emporium.testnet", "250000", 1n]);
    assert.equal(d.nonce, chain.state.nonce + 1n);
    assert.equal(d.publicKey, PUBLIC_KEY);
    assert.equal(shop.seen.paid[0].accepted.payTo, "ada.emporium.testnet");
    assert.match(out, /^Review of 27 chars: add\(\) subtracts\./);
    assert.match(out, /Receipt: paid \$0\.25 in USDC on NEAR testnet, from mybuyer\.testnet to ada\.emporium\.testnet/);
    assert.match(out, /On chain: confirmed/);
    assert.equal(res.structuredContent.paid_with, "own account");
    assert.ok(!JSON.stringify(res).includes(KEY.slice(8)), "the key never leaves the server");
  } finally {
    chain.srv.close();
    shop.srv.close();
  }
});

test("paying from your own account: nothing is signed for a 402 that pays someone else, or a shop on another network", async () => {
  const chain = await fakeChain();
  const evil = await fakeShop({ askPayTo: "evil.testnet" });
  const mainnet = await fakeShop({ network: "near:mainnet" });
  try {
    const env = { EMPORIUM_NEAR_ACCOUNT: "mybuyer.testnet", EMPORIUM_NEAR_KEY: KEY, EMPORIUM_NEAR_RPC: chain.url, EMPORIUM_NEAR_ARCHIVAL_RPC: "" };
    const client = await connect(configFromEnv({ ...env, EMPORIUM_SHOP_URL: evil.url }), globalThis.fetch);
    let res = await client.callTool({ name: "emporium_hire", arguments: { service: "code-review", input: "x" } });
    assert.equal(res.isError, true);
    assert.match(textOf(res), /doesn't match its listed \$0\.25 in USDC to ada\.emporium\.testnet; nothing was signed/);
    assert.equal(evil.seen.paid.length, 0);

    res = await client.callTool({ name: "emporium_hire", arguments: { service: "code-review", input: "x", url: mainnet.url } });
    assert.match(textOf(res), /is paid on near:mainnet, and this server pays on near:testnet \(set EMPORIUM_NETWORK=near:mainnet/);
    assert.equal(mainnet.seen.paid.length, 0);

    const capped = await connect(configFromEnv({ ...env, EMPORIUM_SHOP_URL: evil.url, EMPORIUM_MAX_PRICE: "$0.10" }), globalThis.fetch);
    res = await capped.callTool({ name: "emporium_hire", arguments: { service: "code-review", input: "x" } });
    assert.match(textOf(res), /code-review costs \$0\.25, above this server's \$0\.10 per-job limit/);
  } finally {
    for (const s of [chain, evil, mainnet]) s.srv.close();
  }
});

test("configuration: both keys or neither, testnet unless mainnet is asked for, and the key is never echoed", () => {
  assert.equal(configFromEnv({}).network, "near:testnet");
  assert.equal(configFromEnv({}).wallet, null);
  assert.equal(configFromEnv({}).shopUrl, "https://shop-production-8cc3cf57.up.railway.app");
  assert.equal(configFromEnv({ EMPORIUM_NETWORK: "near:mainnet" }).rpc[0], "https://free.rpc.fastnear.com");
  assert.throws(() => configFromEnv({ EMPORIUM_NEAR_ACCOUNT: "me.testnet" }), /missing: EMPORIUM_NEAR_KEY/);
  assert.throws(() => configFromEnv({ EMPORIUM_NEAR_KEY: KEY }), (e) => e instanceof ConfigError && /missing: EMPORIUM_NEAR_ACCOUNT/.test(e.message) && !e.message.includes(KEY));
  assert.throws(() => configFromEnv({ EMPORIUM_NETWORK: "eip155:8453" }), /near:testnet or near:mainnet/);
  assert.throws(() => configFromEnv({ EMPORIUM_NEAR_ACCOUNT: "me.testnet", EMPORIUM_NEAR_KEY: "secp256k1:abc" }), /ed25519/);
  assert.equal(configFromEnv({ EMPORIUM_SHOP_URL: "127.0.0.1:9502/" }).shopUrl, "http://127.0.0.1:9502");
  assert.equal(toAtomic("$0.30"), 300_000n);
  assert.equal(usd(50_000n), "$0.05");
  assert.equal(usd(1_992_447n), "$1.992447");
});

test("over stdio: `npx emporium-mcp` starts with no keys, and stops with a clear message on half a wallet", async () => {
  const main = fileURLToPath(new URL("../src/main.js", import.meta.url));
  const bad = spawnSync(process.execPath, [main], { env: { ...process.env, EMPORIUM_NEAR_ACCOUNT: "me.testnet", EMPORIUM_NEAR_KEY: "" }, encoding: "utf8", timeout: 20_000 });
  assert.equal(bad.status, 1);
  assert.match(bad.stderr, /emporium-mcp: set both EMPORIUM_NEAR_ACCOUNT and EMPORIUM_NEAR_KEY/);

  const child = spawn(process.execPath, [main], { env: { ...process.env, EMPORIUM_NEAR_ACCOUNT: "", EMPORIUM_NEAR_KEY: "", EMPORIUM_SHOP_URL: "http://127.0.0.1:9" }, stdio: ["pipe", "pipe", "pipe"] });
  const send = (m) => child.stdin.write(JSON.stringify({ jsonrpc: "2.0", ...m }) + "\n");
  let buf = "";
  const tools = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("no tools/list answer in 20 s")), 20_000);
    child.stdout.on("data", (d) => {
      buf += d;
      for (const line of buf.split("\n").slice(0, -1)) {
        const msg = JSON.parse(line);
        if (msg.id === 1) {
          send({ method: "notifications/initialized" });
          send({ id: 2, method: "tools/list" });
        }
        if (msg.id === 2) {
          clearTimeout(timer);
          resolve(msg.result.tools.map((t) => t.name));
        }
      }
      buf = buf.slice(buf.lastIndexOf("\n") + 1);
    });
    send({ id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "t", version: "0" } } });
  });
  child.kill();
  assert.deepEqual(tools, ["emporium_shop", "emporium_hire", "emporium_verify"]);
});
