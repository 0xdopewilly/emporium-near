// Emporium's MCP server: three tools to look at an Emporium shop, hire it, and check its receipt on NEAR.
// With no keys, hires go through the shop's keyless demo (its own testnet account pays); with
// EMPORIUM_NEAR_ACCOUNT and EMPORIUM_NEAR_KEY set, this server pays from that account over x402.
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import * as z from "zod";
import { LIVE_SHOP, NETWORKS, VERSION, ConfigError, normUrl, usd, toAtomic } from "./config.js";
import { NearRpc, checkPayment } from "./near.js";
import { ShopError, catalog, demoHire, feed, paidHire, sha256 } from "./shop.js";

const INSTRUCTIONS = (cfg) => `Emporium turns a Hermes agent into a shop that sells its skills per job, paid in USDC on NEAR over x402, and charged only on delivery: the buyer signs a USDC transfer up front, the shop does the work, and only then does its relayer submit the transfer (paying the gas).

Use emporium_shop to see a shop's services and prices, emporium_hire to have it do a job (you get the deliverable and a NEAR payment receipt), and emporium_verify to confirm a receipt on chain from public NEAR RPC. With no url, the tools use Ada's Emporium on NEAR testnet (${LIVE_SHOP}).

${cfg.wallet ? `Payments come from the NEAR account ${cfg.wallet.account} on ${cfg.network}, at most ${usd(cfg.maxPrice)} per job.` : "No keys are configured, so hires go through the shop's keyless demo: the shop's own testnet demo account pays, a few jobs per caller, no wallet needed."}`;

const text = (t, data, isError = false) => ({
  content: [{ type: "text", text: t }],
  ...(data && { structuredContent: data }),
  ...(isError && { isError: true }),
});

/** Progress notifications, when the client asked for them, so a long job shows what's happening. */
function progress(extra) {
  const token = extra?._meta?.progressToken;
  let n = 0;
  return (message) => {
    if (token === undefined) return;
    extra.sendNotification({ method: "notifications/progress", params: { progressToken: token, progress: ++n, message } }).catch(() => {});
  };
}

const shopUrl = z
  .string()
  .max(300)
  .optional()
  .describe(`The shop's base URL. Omit it for Ada's Emporium, the live shop on NEAR testnet (${LIVE_SHOP}).`);

const BJSON = (v) => JSON.parse(JSON.stringify(v, (_, x) => (typeof x === "bigint" ? x.toString() : x)));

/**
 * @param {ReturnType<import("./config.js").configFromEnv>} cfg
 * @param {{fetch?: typeof fetch}} [deps]
 */
export function createServer(cfg, deps = {}) {
  const fetch = deps.fetch ?? globalThis.fetch;
  const server = new McpServer({ name: "emporium", version: VERSION }, { instructions: INSTRUCTIONS(cfg) });
  const secrets = cfg.wallet ? [cfg.wallet.key, cfg.wallet.key.split(":")[1]] : [];
  const redact = (s) => secrets.reduce((acc, k) => (k ? acc.split(k).join("[redacted]") : acc), String(s));
  const resolve = (url) => normUrl(url?.trim() ? url : cfg.shopUrl);
  // The configured endpoints for the configured network; the public ones for the other.
  const rpcFor = (network) =>
    network === cfg.network ? new NearRpc(cfg.rpc, { archival: cfg.archival, fetch }) : new NearRpc(NETWORKS[network].rpc, { archival: NETWORKS[network].archival, fetch });
  const failure = (e, hint = "") => {
    if (e instanceof ShopError || e instanceof ConfigError) return text(`${redact(e.message)}${hint}`, { error: redact(e.message), ...(e.status && { http_status: e.status }) }, true);
    return text(`Something went wrong: ${redact(e?.message ?? e)}${hint}`, { error: redact(e?.message ?? e) }, true);
  };

  server.registerTool(
    "emporium_shop",
    {
      title: "An Emporium shop's menu",
      description: `Reads an Emporium shop's public catalog: its name, each service it sells (id, title, price in USDC, what it does, an example input, the most input it takes), the network and account it is paid to, its relayer, whether its keyless demo is on (and how many demo jobs are left today), and its own sales numbers. Read-only, no keys. Use it before emporium_hire to pick a service id. Without a url it reads Ada's Emporium, the live shop on NEAR testnet (${LIVE_SHOP}), whose services are a launch post in English and Yoruba (it hires a second shop for the Yoruba), a code review and a web research brief.`,
      inputSchema: { url: shopUrl },
      annotations: { title: "An Emporium shop's menu", readOnlyHint: true, openWorldHint: true },
    },
    async ({ url }) => {
      try {
        const base = resolve(url);
        const [cat, stats] = await Promise.all([catalog(base, { fetch }), feed(base, { fetch })]);
        const net = NETWORKS[cat.network];
        const demo = cat.demo?.enabled ? cat.demo : null;
        const lines = [
          `${cat.name} (${base}): ${cat.tagline ?? ""}`.trim(),
          `Paid in USDC on ${cat.network_label ?? cat.network} to ${cat.pay_to}${cat.relayer ? `; its relayer ${cat.relayer} submits payments and pays the gas` : ""}.${cat.charged ? ` Charged ${cat.charged}.` : ""}`,
          cat.open === false ? "The shop is closed right now." : "",
          stats ? `Its own numbers: ${stats.jobs_settled} jobs delivered, ${stats.earned_total} earned${stats.p50_seconds != null ? `, median delivery ${stats.p50_seconds} s` : ""}${stats.not_charged ? `, ${stats.not_charged} not charged` : ""}.` : "",
          "",
          "Services (pass the id to emporium_hire):",
          ...cat.services.map((s) => {
            const sub = (s.subcontracts ?? []).length ? ` It hires ${s.subcontracts.map((p) => p.service).join(", ")} from another shop for part of the job.` : "";
            const max = s.input?.properties?.input?.maxLength;
            return `- ${s.id} (${s.price}): ${s.title}. ${s.description ?? ""}${sub}${s.example ? ` Example input: ${JSON.stringify(s.example)}` : ""}${max ? ` (up to ${max} characters)` : ""}`;
          }),
          "",
          demo
            ? `Keyless demo: on. ${demo.daily_left} of ${demo.daily_jobs} demo jobs left today (UTC), ${demo.per_address} per caller, services up to ${demo.max_price}, inputs up to ${String(demo.input?.input ?? "").replace(/^up to /, "")}. The shop's demo account ${demo.payer} pays.`
            : "Keyless demo: off on this shop; hiring it needs your own NEAR account (EMPORIUM_NEAR_ACCOUNT and EMPORIUM_NEAR_KEY).",
          cfg.wallet
            ? `This server pays from ${cfg.wallet.account} on ${cfg.network}, at most ${usd(cfg.maxPrice)} a job.${cat.network !== cfg.network ? ` It won't pay this shop: the shop is on ${cat.network}.` : ""}`
            : demo
              ? "This server has no keys, so emporium_hire uses the keyless demo."
              : "",
        ].filter((l, i, a) => l !== "" || (a[i - 1] ?? "") !== "");
        return text(lines.join("\n").trim(), {
          url: base,
          name: cat.name,
          network: cat.network,
          pay_to: cat.pay_to,
          relayer: cat.relayer ?? null,
          asset_contract: cat.asset_contract ?? net?.usdc ?? null,
          open: cat.open !== false,
          services: cat.services.map((s) => ({ id: s.id, title: s.title, price: s.price, description: s.description, example: s.example, max_input_chars: s.input?.properties?.input?.maxLength ?? null })),
          demo: demo ? { enabled: true, daily_left: demo.daily_left, daily_jobs: demo.daily_jobs, per_address: demo.per_address, max_price: demo.max_price, payer: demo.payer } : { enabled: false },
          stats: stats ? { jobs_settled: stats.jobs_settled, earned_total: stats.earned_total, p50_seconds: stats.p50_seconds } : null,
          pays_with: cfg.wallet ? { account: cfg.wallet.account, network: cfg.network } : "keyless demo",
        });
      } catch (e) {
        return failure(e);
      }
    },
  );

  server.registerTool(
    "emporium_hire",
    {
      title: "Hire an Emporium shop",
      description: `Has an Emporium shop do one job and returns the deliverable with its NEAR payment receipt (transaction hash, explorer link, amount, payer, payee, the relayer that settled it, and a SHA-256 of the deliverable), then checks that receipt on chain.

How it pays: ${cfg.wallet ? `from your NEAR account ${cfg.wallet.account} on ${cfg.network}, over x402 \`exact\` on NEAR with the official @x402/near client: the shop answers 402, the account signs a NEP-366 delegate holding one USDC transfer of the listed price to the shop (at most ${usd(cfg.maxPrice)}; it needs no NEAR for gas), the shop does the job, and only then its relayer submits the transfer.${cfg.network === "near:mainnet" ? " This is mainnet: it spends real USDC." : ""}` : "with no keys, through the shop's keyless demo (POST /demo/hire): the shop's own NEAR testnet demo account pays its listed price over the same x402 path, settled on chain after delivery. The demo allows a few jobs per caller every 10 minutes and a daily total (emporium_shop shows what's left); set EMPORIUM_NEAR_ACCOUNT and EMPORIUM_NEAR_KEY to pay from your own account instead."} A job that isn't delivered is never paid.

It waits for the job: usually 20 to 90 seconds, up to a few minutes for a web research brief. Get service ids from emporium_shop. On Ada's Emporium (the default, NEAR testnet): "code-review" ($0.25) takes code, "launch-post" ($0.30) takes a product description and returns an English post plus a Yoruba translation bought from a second shop, "web-brief" ($0.40) takes a question. Side effects: a real NEAR testnet USDC payment, and the input is sent to the shop's agent (send nothing private).`,
      inputSchema: {
        service: z.string().min(1).max(48).describe('The service id from emporium_shop, e.g. "code-review".'),
        input: z.string().min(1).max(8000).describe("The material for the job: the code to review, the product to announce, the question to research. The keyless demo takes up to 2,000 characters."),
        url: shopUrl,
      },
      annotations: { title: "Hire an Emporium shop", readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
    },
    async ({ service, input, url }, extra) => {
      const report = progress(extra);
      let base, cat;
      try {
        base = resolve(url);
        cat = await catalog(base, { fetch });
      } catch (e) {
        return failure(e);
      }
      let out;
      const mode = cfg.wallet ? "own account" : "keyless demo";
      try {
        if (cfg.wallet) {
          report(`Asking ${cat.name} for its price`);
          out = await paidHire(base, cat, service, input, { wallet: cfg.wallet, network: cfg.network, maxPrice: cfg.maxPrice, rpcUrl: cfg.rpc[0], fetch, signal: extra?.signal, onProgress: report });
        } else {
          if (cat.network === "near:mainnet") throw new ShopError(`${cat.name} is on NEAR mainnet, where there is no keyless demo; set EMPORIUM_NEAR_ACCOUNT, EMPORIUM_NEAR_KEY and EMPORIUM_NETWORK=near:mainnet to pay it from your own account`);
          if (!cat.demo?.enabled) throw new ShopError(`${cat.name} has no keyless demo`, { status: 403 });
          report(`${cat.name} is doing the job; its demo account pays only once the result is ready`);
          out = await demoHire(base, cat, service, input, { fetch, signal: extra?.signal });
        }
      } catch (e) {
        const hint = cfg.wallet
          ? ""
          : e?.status === 403 && /keyless demo/.test(e.message)
            ? "\n\nThis shop's keyless demo is off. Set EMPORIUM_NEAR_ACCOUNT and EMPORIUM_NEAR_KEY (a NEAR testnet account holding a little testnet USDC) to pay it from your own account."
            : e?.status === 429
              ? "\n\nThe keyless demo is rate-limited. To hire without waiting, set EMPORIUM_NEAR_ACCOUNT and EMPORIUM_NEAR_KEY to pay from your own NEAR testnet account."
              : "";
        return failure(e, hint);
      }

      const r = out.receipt ?? {};
      const network = r.network ?? cat.network;
      const net = NETWORKS[network];
      const digestOk = r.output_sha256 ? sha256(out.output) === r.output_sha256 : null;
      let onchain = null;
      if (r.tx && r.settled_by && net) {
        report("Checking the receipt on NEAR");
        try {
          const want = toAtomic(r.paid ?? r.amount);
          const c = await checkPayment(rpcFor(network), { tx: r.tx, signer: r.settled_by, usdc: net.usdc, payTo: r.pay_to ?? cat.pay_to, payer: r.payer ?? "" });
          onchain = { confirmed: c.succeeded && want !== null && c.to_shop >= want, ...BJSON(c), to_shop: usd(c.to_shop) };
        } catch (e) {
          onchain = { confirmed: null, error: redact(e?.message ?? e) };
        }
      }
      const payer = r.payer ?? (cfg.wallet ? cfg.wallet.account : out.demo?.payer);
      const receiptLines = [
        `Receipt: paid ${r.paid ?? r.amount} in USDC on ${net?.label ?? network}, from ${payer}${mode === "keyless demo" ? " (the shop's keyless demo account)" : ""} to ${r.pay_to ?? cat.pay_to}, settled after delivery${r.settled_by ? ` by the shop's relayer ${r.settled_by}, which paid the gas` : ""}.`,
        r.tx ? `Transaction: ${r.tx}${r.explorer ? `  ${r.explorer}` : ""}` : "No transaction hash came back.",
        r.output_sha256 ? `SHA-256 of the deliverable: ${r.output_sha256}${digestOk ? " (matches the text above)" : digestOk === false ? " (does NOT match the text above)" : ""}` : "",
        onchain
          ? onchain.confirmed
            ? `On chain: confirmed. ${onchain.to_shop} in USDC reached ${r.pay_to ?? cat.pay_to}${onchain.block ? ` in block ${onchain.block.height} (${onchain.block.time})` : ""}, and every receipt of the transaction succeeded.`
            : onchain.confirmed === null
              ? `On chain: couldn't check right now (${onchain.error}); run emporium_verify with tx ${r.tx} later.`
              : `On chain: NOT confirmed (${onchain.succeeded ? `${onchain.to_shop} reached the shop` : "a receipt failed"}). Run emporium_verify with tx ${r.tx} for details.`
          : "",
        out.demo && Number.isFinite(out.demo.daily_left) ? `Keyless demo jobs left today: ${out.demo.daily_left}.` : "",
      ].filter(Boolean);
      return text([out.output.trim(), "", "---", ...receiptLines].join("\n"), {
        shop: out.shop ?? cat.name,
        url: base,
        service,
        output: out.output,
        receipt: r,
        paid_with: mode,
        output_sha256_matches: digestOk,
        onchain,
        ...(out.demo && { demo: out.demo }),
      });
    },
  );

  server.registerTool(
    "emporium_verify",
    {
      title: "Check an Emporium receipt on NEAR",
      description: `Looks a payment receipt up on NEAR from public RPC (FastNEAR, then dRPC; an archival node for transactions older than about 30 hours) and reports what the chain says: whether every receipt of the transaction succeeded, each USDC transfer in it (from, to, amount), how much USDC reached the shop, the block and its time. Read-only, no keys. Pass the tx from an emporium_hire receipt. A NEAR transaction is found by its hash and the account that signed it, which for an Emporium payment is the shop's relayer (the receipt's settled_by); without account, the relayer named in the shop's catalog is used.`,
      inputSchema: {
        tx: z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{43,44}$/, "a NEAR transaction hash: 43 or 44 base58 characters").describe("The transaction hash from the receipt (its tx)."),
        account: z.string().max(64).optional().describe("The account that signed the transaction: the receipt's settled_by (the shop's relayer). Defaults to the relayer in the shop's catalog."),
        url: shopUrl,
        network: z.enum(["near:testnet", "near:mainnet"]).optional().describe("The network. Defaults to the shop's network."),
      },
      annotations: { title: "Check an Emporium receipt on NEAR", readOnlyHint: true, openWorldHint: true },
    },
    async ({ tx, account, url, network }) => {
      let cat = null;
      let base;
      try {
        base = resolve(url);
        cat = await catalog(base, { fetch });
      } catch {
        cat = null; // the chain is the authority; the catalog only fills in defaults
      }
      const net = network ?? cat?.network ?? cfg.network;
      const signer = account?.trim() || cat?.relayer;
      if (!NETWORKS[net]) return text(`${net} isn't a NEAR network this server reads.`, { error: "unsupported network" }, true);
      if (!signer) return text("Pass account: the account that signed the transaction (the receipt's settled_by). The shop's catalog couldn't be read to find its relayer.", { error: "no signer" }, true);
      const usdc = NETWORKS[net].usdc;
      const payTo = cat?.network === net ? String(cat.pay_to ?? "") : "";
      let c;
      try {
        c = await checkPayment(rpcFor(net), { tx, signer, usdc, payTo });
      } catch (e) {
        const unknown = /UNKNOWN_TRANSACTION|doesn't exist|does not exist/.test(String(e?.message));
        return text(
          unknown
            ? `NEAR ${NETWORKS[net].label} has no transaction ${tx} signed by ${signer}. Check the hash and the signer (the receipt's settled_by), and the network.`
            : `Couldn't read ${tx} from NEAR RPC: ${e?.message ?? e}`,
          { confirmed: false, tx, signer, network: net, error: String(e?.message ?? e) },
          true,
        );
      }
      const usdcMoves = c.transfers.map((t) => `${usd(BigInt(t.amount)).slice(1)} USDC from ${t.from} to ${t.to}`);
      const confirmed = c.succeeded && (payTo ? c.to_shop > 0n : c.transfers.length > 0);
      const lines = [
        confirmed
          ? `Confirmed on ${NETWORKS[net].label}: ${payTo ? `${usd(c.to_shop)} in USDC reached ${payTo}${cat?.name ? ` (${cat.name})` : ""}` : usdcMoves.join("; ")}.`
          : c.succeeded
            ? `The transaction ran, but ${payTo ? `no USDC reached ${payTo}` : "it moved no USDC"}.`
            : "NOT a payment: a receipt of this transaction failed, so no USDC moved.",
        `Transaction ${tx}, signed by ${c.signer} (the relayer, which paid the gas) for ${c.receiver}${c.block ? `, in block ${c.block.height} at ${c.block.time}` : ""}${c.source === "archival" ? " (read from an archival node)" : ""}.`,
        c.transfers.length ? `USDC transfers in it (token ${usdc}): ${usdcMoves.join("; ")}.` : "No USDC transfer events in it.",
        c.failures.length ? `Failures: ${JSON.stringify(c.failures).slice(0, 400)}` : "Every receipt succeeded.",
        `${NETWORKS[net].explorer}${tx}`,
      ];
      return text(lines.join("\n"), { confirmed, network: net, explorer: NETWORKS[net].explorer + tx, ...BJSON(c), to_shop: usd(c.to_shop), pay_to: payTo || null });
    },
  );

  return server;
}
