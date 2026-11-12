// The tamper matrix: payloads built with near-api-js 7.4.0 (NEAR's own JS library, neither of the two
// implementations under test), each wrong in one way, each run through both verifiers on the same bytes.
//
//   expect "reject": a tampered payment; both verifiers must refuse it.
//   expect "accept": a control; the same construction, untampered (or harmlessly varied), must pass both.
//   expect "edge":   a case the spec doesn't pin down, where the two implementations may answer
//                    differently; the chain is asked what really happens (run.mjs --chain-truth).
import * as N from "near-api-js";
import { createHash, randomBytes } from "node:crypto";

export const TGAS = 10n ** 12n;
const sha256 = (b) => createHash("sha256").update(b).digest();
const utf8 = (s) => new TextEncoder().encode(s);
export const timeoutBlocks = (s) => BigInt(Math.max(1, Math.ceil(Number(s) / 1)));

// t: { rpc, network, usdc, buyer, buyerSecret, other, relayer, relayerSecret, payTo, amount, unregistered,
//      notContract, missingAccount, record? }
export function caseTools(t) {
  const kpOf = (secret) => N.KeyPair.fromString(secret);

  async function chainState(account, publicKey) {
    const [ak, block] = await Promise.all([
      t.rpc("query", { request_type: "view_access_key", finality: "final", account_id: account, public_key: publicKey }),
      t.rpc("block", { finality: "final" }),
    ]);
    return { nonce: BigInt(ak.nonce), head: BigInt(block.header.height) };
  }

  // A payment for `amount` to `payTo`, signed by the buyer, valid unless `o` says otherwise.
  async function pay(o = {}) {
    const amount = o.amount ?? t.amount;
    const req = {
      scheme: "exact", network: t.network, amount, asset: t.usdc, payTo: t.payTo, maxTimeoutSeconds: 300, extra: {},
      ...(o.req ?? {}),
    };
    const signKey = kpOf(o.keySecret ?? t.buyerSecret);
    const fieldKey = o.fieldKey ?? signKey; // the public key written into the delegate
    const st = await chainState(t.buyer, kpOf(t.buyerSecret).getPublicKey().toString());
    const args = o.rawArgs ? utf8(o.rawArgs) : (o.args ?? { receiver_id: req.payTo, amount: req.amount });
    const actions = o.actions ?? [N.actions.functionCall(o.method ?? "ft_transfer", args, o.gas ?? 30n * TGAS, o.deposit ?? 1n)];
    const da = N.buildDelegateAction({
      actions,
      maxBlockHeight: o.maxBlockHeight ? o.maxBlockHeight(st) : st.head + timeoutBlocks(req.maxTimeoutSeconds),
      nonce: o.nonce ? o.nonce(st) : st.nonce + 1n,
      publicKey: fieldKey.getPublicKey(),
      receiverId: o.receiver ?? req.asset,
      senderId: o.sender ?? t.buyer,
    });
    const message = N.encodeDelegateAction(da); // NEP-461 prefix + the delegate's Borsh
    let bytes;
    if (o.keySecret === undefined && o.fieldKey === undefined && !o.signOver) {
      const { signedDelegate } = await new N.KeyPairSigner(signKey).signDelegateAction(da);
      bytes = Buffer.from(N.encodeSignedDelegate(signedDelegate));
    } else {
      const signed = o.signOver ? o.signOver(st) : message; // sign some other delegate's bytes
      const { signature } = signKey.sign(sha256(signed));
      bytes = Buffer.concat([Buffer.from(message.slice(4)), Buffer.from([fieldKey.getPublicKey().keyType]), Buffer.from(signature)]);
    }
    if (o.bytes) bytes = o.bytes(bytes, message);
    const sda = o.sda ? o.sda(bytes) : bytes.toString("base64");
    const accepted = { ...req, ...(o.accepted ?? {}) };
    const payload = o.payload ? o.payload(sda) : { signedDelegateAction: sda };
    return {
      paymentPayload: { x402Version: o.x402Version ?? 2, resource: { url: "https://interop.example/job", description: "tamper matrix", mimeType: "application/json" }, accepted, payload },
      paymentRequirements: req,
      state: st,
    };
  }
  return { pay, chainState, kpOf };
}

export function catalogue(t) {
  const A = t.amount;
  const minus1 = (BigInt(A) - 1n).toString();
  const plus1 = (BigInt(A) + 1n).toString();
  const fc = (method, args, gas = 30n * TGAS, deposit = 1n) => N.actions.functionCall(method, args, gas, deposit);
  const transfer = { receiver_id: t.payTo, amount: A };
  const flipLastBit = (b) => { const c = Buffer.from(b); c[c.length - 1] ^= 1; return c; };
  const R = "reject", OK = "accept", EDGE = "edge";
  return [
    // ---- controls ----
    { id: "C01", cls: "control", expect: OK, title: "untampered payment (near-api-js signs it)", o: {} },
    { id: "C02", cls: "control", expect: OK, title: "with a string memo", o: { args: { ...transfer, memo: "order 42" } } },
    { id: "C03", cls: "control", expect: OK, title: "with memo: null", o: { args: { ...transfer, memo: null } } },
    { id: "C04", cls: "control", expect: OK, title: "args keys reordered, with whitespace", o: { rawArgs: `{ "amount": "${A}", "receiver_id": "${t.payTo}" }` } },
    { id: "C05", cls: "control", expect: OK, title: "gas exactly at the 100 TGas cap", o: { gas: 100n * TGAS } },
    { id: "C06", cls: "control", expect: OK, title: "maxTimeoutSeconds 3600, window used in full", o: { req: { maxTimeoutSeconds: 3600 } } },
    { id: "C07", cls: "control", expect: OK, title: "signed by hand: ed25519 over sha256(NEP-461 prefix + delegate)", o: { keySecret: t.buyerSecret } },

    // ---- amount ----
    { id: "T01", cls: "wrong amount", expect: R, title: "transfer is 1 unit short", o: { args: { ...transfer, amount: minus1 } } },
    { id: "T02", cls: "wrong amount", expect: R, title: "transfer is 1 unit over", o: { args: { ...transfer, amount: plus1 } } },
    { id: "T03", cls: "wrong amount", expect: R, title: "accepted.amount differs from the requirements", o: { accepted: { amount: minus1 } } },
    { id: "T04", cls: "wrong amount", expect: R, title: "amount with a leading zero", o: { args: { ...transfer, amount: "0" + A } } },
    { id: "T05", cls: "wrong amount", expect: R, title: "negative amount", o: { args: { ...transfer, amount: "-" + A } } },
    { id: "T06", cls: "wrong amount", expect: R, title: "amount missing from the args", o: { args: { receiver_id: t.payTo } } },
    // ---- payTo ----
    { id: "T07", cls: "wrong payTo", expect: R, title: "transfer pays another account", o: { args: { ...transfer, receiver_id: t.other } } },
    { id: "T08", cls: "wrong payTo", expect: R, title: "accepted.payTo differs from the requirements", o: { accepted: { payTo: t.other } } },
    { id: "T09", cls: "wrong payTo", expect: R, title: "receiver_id differs only in case", o: { args: { ...transfer, receiver_id: t.payTo.toUpperCase() } } },
    { id: "T10", cls: "wrong payTo", expect: R, title: "receiver_id is a number", o: { args: { receiver_id: 7, amount: A } } },
    // ---- asset / token contract ----
    { id: "T11", cls: "wrong asset", expect: R, title: "delegate calls another token contract", o: { receiver: "usdc.fakes.testnet" } },
    { id: "T12", cls: "wrong asset", expect: R, title: "accepted.asset differs from the requirements", o: { accepted: { asset: "usdc.fakes.testnet" } } },
    { id: "T13", cls: "wrong asset", expect: R, title: "requirements name an account with no contract", o: { req: { asset: t.notContract }, receiver: t.notContract } },
    { id: "T14", cls: "wrong asset", expect: R, title: "requirements name an account that doesn't exist", o: { req: { asset: t.missingAccount }, receiver: t.missingAccount } },
    // ---- method / actions ----
    { id: "T15", cls: "wrong method", expect: R, title: "ft_transfer_call instead of ft_transfer", o: { method: "ft_transfer_call", args: { ...transfer, msg: "" } } },
    { id: "T16", cls: "wrong method", expect: R, title: "storage_deposit", o: { method: "storage_deposit", args: { account_id: t.payTo } } },
    { id: "T17", cls: "wrong method", expect: R, title: "Ft_transfer (case)", o: { method: "Ft_transfer" } },
    { id: "T18", cls: "extra action", expect: R, title: "two ft_transfers", o: { actions: [fc("ft_transfer", transfer), fc("ft_transfer", transfer)] } },
    { id: "T19", cls: "extra action", expect: R, title: "ft_transfer + Transfer of 1 NEAR", o: { actions: [fc("ft_transfer", transfer), N.actions.transfer(10n ** 24n)] } },
    { id: "T20", cls: "extra action", expect: R, title: "ft_transfer + AddKey (full access) for an attacker key", o: { actions: [fc("ft_transfer", transfer), N.actions.addFullAccessKey(N.KeyPair.fromRandom("ed25519").getPublicKey())] } },
    { id: "T21", cls: "extra action", expect: R, title: "no actions at all", o: { actions: [] } },
    { id: "T22", cls: "extra action", expect: R, title: "a lone Transfer instead of a function call", o: { actions: [N.actions.transfer(1n)] } },
    // ---- deposit ----
    { id: "T23", cls: "deposit != 1 yocto", expect: R, title: "deposit 0", o: { deposit: 0n } },
    { id: "T24", cls: "deposit != 1 yocto", expect: R, title: "deposit 2 yocto", o: { deposit: 2n } },
    { id: "T25", cls: "deposit != 1 yocto", expect: R, title: "deposit 1 NEAR (the relayer would pay it)", o: { deposit: 10n ** 24n } },
    // ---- gas ----
    { id: "T26", cls: "excess gas", expect: R, title: "100 TGas + 1 gas", o: { gas: 100n * TGAS + 1n } },
    { id: "T27", cls: "excess gas", expect: R, title: "300 TGas", o: { gas: 300n * TGAS } },
    { id: "T28", cls: "excess gas", expect: R, title: "u64 max gas", o: { gas: 2n ** 64n - 1n } },
    // ---- expiry / window ----
    { id: "T29", cls: "expired max_block_height", expect: R, title: "max_block_height = head - 1", o: { maxBlockHeight: (s) => s.head - 1n } },
    { id: "T30", cls: "expired max_block_height", expect: R, title: "max_block_height = 1", o: { maxBlockHeight: () => 1n } },
    { id: "T31", cls: "window > maxTimeoutSeconds", expect: R, title: "max_block_height 100 blocks past the window", o: { maxBlockHeight: (s) => s.head + 400n } },
    { id: "T32", cls: "window > maxTimeoutSeconds", expect: R, title: "max_block_height = u64 max", o: { maxBlockHeight: () => 2n ** 64n - 1n } },
    // ---- signature ----
    { id: "T33", cls: "bad signature", expect: R, title: "one bit of the signature flipped", o: { bytes: flipLastBit } },
    { id: "T34", cls: "bad signature", expect: R, title: "signed by another key than the one it names", o: { keySecret: t.otherSecret, fieldKey: N.KeyPair.fromString(t.buyerSecret) } },
    { id: "T35", cls: "bad signature", expect: R, title: "a valid signature of a different delegate (amount 1)", o: { signOver: (s) => N.encodeDelegateAction(N.buildDelegateAction({ actions: [fc("ft_transfer", { receiver_id: t.payTo, amount: "1" })], maxBlockHeight: s.head + 300n, nonce: s.nonce + 1n, publicKey: N.KeyPair.fromString(t.buyerSecret).getPublicKey(), receiverId: t.usdc, senderId: t.buyer })), keySecret: t.buyerSecret } },
    { id: "T36", cls: "bad signature", expect: R, title: "signature all zeros", o: { bytes: (b) => Buffer.concat([b.subarray(0, b.length - 64), Buffer.alloc(64)]) } },
    // ---- signer / key ----
    { id: "T37", cls: "signer != key owner", expect: R, title: "sender is another account; key is the buyer's", o: { sender: t.other } },
    { id: "T38", cls: "signer != key owner", expect: R, title: "a fresh key the buyer never added", o: { keySecret: N.KeyPair.fromRandom("ed25519").toString() } },
    { id: "T39", cls: "signer != key owner", expect: R, title: "sender account doesn't exist", o: { sender: t.missingAccount } },
    { id: "T40", cls: "signer != key owner", expect: R, title: "the relayer as payer", o: { sender: t.relayer, keySecret: t.relayerSecret } },
    { id: "T41", cls: "signer != key owner", expect: R, title: "a secp256k1 key the buyer never added", o: { keySecret: N.KeyPair.fromRandom("secp256k1").toString() } },
    // ---- nonce ----
    { id: "T42", cls: "stale nonce", expect: R, title: "nonce = the key's current nonce (already used)", o: { nonce: (s) => s.nonce } },
    { id: "T43", cls: "stale nonce", expect: R, title: "nonce 1000 below the key's", o: { nonce: (s) => s.nonce - 1000n } },
    { id: "T44", cls: "stale nonce", expect: R, title: "nonce above NEAR's bound (head * 1e6), with a day of margin", o: { nonce: (s) => (s.head + 150_000n) * 1_000_000n } },
    // ---- network / version / scheme ----
    { id: "T45", cls: "wrong network", expect: R, title: "requirements near:mainnet, payload near:testnet", o: { req: { network: "near:mainnet" }, accepted: { network: t.network } } },
    { id: "T46", cls: "wrong network", expect: R, title: "payload near:mainnet, requirements near:testnet", o: { accepted: { network: "near:mainnet" } } },
    { id: "T47", cls: "wrong network", expect: R, title: "near:betanet", o: { req: { network: "near:betanet" } } },
    { id: "T48", cls: "wrong network", expect: R, title: "an EVM network (eip155:84532)", o: { req: { network: "eip155:84532" } } },
    { id: "T49", cls: "wrong version/scheme", expect: R, title: "x402Version 1", o: { x402Version: 1 } },
    { id: "T50", cls: "wrong version/scheme", expect: R, title: "scheme upto", o: { req: { scheme: "upto" } } },
    { id: "T51", cls: "bad requirements", expect: R, title: "maxTimeoutSeconds 0", o: { req: { maxTimeoutSeconds: 0 }, maxBlockHeight: (s) => s.head + 1n } },
    { id: "T52", cls: "bad requirements", expect: R, title: "maxTimeoutSeconds -60", o: { req: { maxTimeoutSeconds: -60 }, maxBlockHeight: (s) => s.head + 1n } },
    // ---- chain state ----
    { id: "T53", cls: "chain state", expect: R, title: "payer's balance is below the amount", o: { amount: "1000000000000000" } },
    { id: "T54", cls: "chain state", expect: R, title: "payTo isn't registered with the token", o: { req: { payTo: t.unregistered } } },
    // ---- malformed / non-delegate payloads ----
    { id: "M01", cls: "malformed payload", expect: R, title: "signedDelegateAction isn't base64", o: { sda: () => "@@ not base64 @@" } },
    { id: "M02", cls: "malformed payload", expect: R, title: "signedDelegateAction is empty", o: { sda: () => "" } },
    { id: "M03", cls: "malformed payload", expect: R, title: "200 random bytes", o: { sda: () => randomBytes(200).toString("base64") } },
    { id: "M04", cls: "malformed payload", expect: R, title: "truncated by 7 bytes", o: { bytes: (b) => b.subarray(0, b.length - 7) } },
    { id: "M05", cls: "malformed payload", expect: R, title: "an unsigned delegate (NEP-461 message, no signature)", o: { bytes: (_, msg) => Buffer.from(msg) } },
    { id: "M06", cls: "malformed payload", expect: R, title: "a signed relayer transaction instead of a delegate", o: { bytes: (b) => relayerTx(t, b) } },
    { id: "M07", cls: "malformed payload", expect: R, title: "payload is {}", o: { payload: () => ({}) } },
    { id: "M08", cls: "malformed payload", expect: R, title: "signedDelegateAction is a number", o: { payload: () => ({ signedDelegateAction: 12345 }) } },
    { id: "M09", cls: "malformed payload", expect: R, title: "the delegate as JSON, base64'd", o: { sda: () => Buffer.from(JSON.stringify({ senderId: t.buyer, receiverId: t.usdc })).toString("base64") } },
    { id: "M10", cls: "malformed payload", expect: R, title: "unknown signature type byte (7)", o: { bytes: (b) => { const c = Buffer.from(b); c[c.length - 65] = 7; return c; } } },
    { id: "M11", cls: "malformed payload", expect: R, title: "args aren't JSON (bytes ff fe)", o: { args: new Uint8Array([0xff, 0xfe]) } },
    { id: "M12", cls: "malformed payload", expect: R, title: "args are a JSON array", o: { rawArgs: `["${t.payTo}","${A}"]` } },
    { id: "M13", cls: "malformed payload", expect: R, title: "payload carries the base64 in another field", o: { payload: (sda) => ({ signedDelegate: sda }) } },

    // ---- edges: the spec doesn't pin these down; the chain decides ----
    { id: "E01", cls: "edge", expect: EDGE, title: "amount as a JSON number, not a string", o: { rawArgs: `{"receiver_id":"${t.payTo}","amount":${A}}` }, truth: true },
    { id: "E02", cls: "edge", expect: EDGE, title: "duplicate receiver_id key (another account first, payTo last)", o: { rawArgs: `{"receiver_id":"${t.other}","receiver_id":"${t.payTo}","amount":"${A}"}` }, truth: true },
    { id: "E03", cls: "edge", expect: EDGE, title: "duplicate amount key (\"1\" first, the amount last)", o: { rawArgs: `{"receiver_id":"${t.payTo}","amount":"1","amount":"${A}"}` }, truth: true },
    { id: "E04", cls: "edge", expect: EDGE, title: "memo is a number", o: { args: { ...transfer, memo: 42 } }, truth: true },
    { id: "E05", cls: "edge", expect: EDGE, title: "an extra unknown argument (msg)", o: { args: { ...transfer, msg: "hello" } }, truth: true },
    { id: "E06", cls: "edge", expect: EDGE, title: "gas 0", o: { gas: 0n }, truth: true },
    { id: "E07", cls: "edge", expect: EDGE, title: "gas 1 TGas", o: { gas: 1n * TGAS }, truth: true },
    { id: "E08", cls: "edge", expect: EDGE, title: "a trailing byte after the signature", o: { bytes: (b) => Buffer.concat([b, Buffer.from([0])]) }, truth: true },
    { id: "E09", cls: "edge", expect: EDGE, title: "requirements amount \"0\" (and a transfer of 0)", o: { amount: "0" }, truth: true },
  ];
}

function relayerTx(t, signedDelegateBytes) {
  const kp = N.KeyPair.fromString(t.relayerSecret);
  const tx = N.createTransaction(t.relayer, kp.getPublicKey(), t.buyer, 1n, [N.actions.transfer(1n)], new Uint8Array(32).fill(9));
  const body = N.encodeTransaction(tx);
  const { signature } = kp.sign(sha256(body));
  return Buffer.concat([Buffer.from(body), Buffer.from([0]), Buffer.from(signature)]);
}
