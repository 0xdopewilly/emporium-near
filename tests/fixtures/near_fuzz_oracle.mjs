// Fuzz oracle for scripts/check_borsh.py --fuzz: encodes and signs many random delegates and relayer
// transactions with near-api-js, one JSON line out per case in.
//
//   NEARJS=<folder>/node_modules/near-api-js/lib/index.js node tests/fixtures/near_fuzz_oracle.mjs cases.json
//
// cases.json is a list of {buyer, relayer (secret keys), sender, receiver, nonce, mbh, actions: [{method,
// json | raw (base64), gas, deposit}], relayer_id, tx_receiver, tx_nonce, block_hash (base64)}; integers are
// decimal strings. Each output line has the same fields near_oracle.mjs prints (less the public key).
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const N = await import(process.env.NEARJS);
const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));
const b64 = (u) => Buffer.from(u).toString("base64");
const out = [];
for (const c of cases) {
  const kp = N.KeyPair.fromString(c.buyer);
  const actions = c.actions.map((a) => N.actions.functionCall(
    a.method, a.raw != null ? new Uint8Array(Buffer.from(a.raw, "base64")) : a.json, BigInt(a.gas), BigInt(a.deposit)));
  const da = N.buildDelegateAction({
    actions, maxBlockHeight: BigInt(c.mbh), nonce: BigInt(c.nonce), publicKey: kp.getPublicKey(),
    receiverId: c.receiver, senderId: c.sender,
  });
  const encDA = N.encodeDelegateAction(da);
  const { delegateHash, signedDelegate } = await new N.KeyPairSigner(kp).signDelegateAction(da);
  const encSD = N.encodeSignedDelegate(signedDelegate);
  const rkp = N.KeyPair.fromString(c.relayer);
  const tx = N.createTransaction(c.relayer_id, rkp.getPublicKey(), c.tx_receiver, BigInt(c.tx_nonce),
    [N.actions.signedDelegate(signedDelegate)], new Uint8Array(Buffer.from(c.block_hash, "base64")));
  const encTx = N.encodeTransaction(tx);
  const signed = await new N.KeyPairSigner(rkp).signTransaction(tx);
  const signedTx = Array.isArray(signed) ? signed[1] : (signed.signedTransaction ?? signed);
  out.push(JSON.stringify({
    delegate_action_b64: b64(encDA),
    nep461_hash_hex: Buffer.from(delegateHash).toString("hex"),
    signed_delegate_b64: b64(encSD),
    tx_b64: b64(encTx),
    tx_hash_b58: N.baseEncode(createHash("sha256").update(encTx).digest()),
    signed_tx_b64: b64(N.encodeTransaction(signedTx)),
  }));
}
process.stdout.write(out.join("\n") + "\n");
