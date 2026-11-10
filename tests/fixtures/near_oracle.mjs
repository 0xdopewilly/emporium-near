// Test oracle: encodes and signs the delegate + relayer transaction in near_vectors.json with
// near-api-js, so tests/test_near.py can pin near.py's Borsh to the bytes NEAR's own JS library makes.
//
//   npm i near-api-js@7.4.0                       # in any folder, then from the plugin's root:
//   NEARJS=<that folder>/node_modules/near-api-js/lib/index.js node tests/fixtures/near_oracle.mjs \
//     "$(python -c 'import hashlib,near; print("ed25519:"+near.b58encode(hashlib.sha256(b"emporium-near-test-buyer").digest()))')" \
//     "$(python -c 'import hashlib,near; print("ed25519:"+near.b58encode(hashlib.sha256(b"emporium-near-test-relayer").digest()))')"
//
// It prints the vectors as JSON (the last seven fields of near_vectors.json).
import { createHash } from "node:crypto";
const N = await import(process.env.NEARJS);
const [buyerSecret, relayerSecret] = process.argv.slice(2);
const USDC = "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af";
const kp = N.KeyPair.fromString(buyerSecret);
const da = N.buildDelegateAction({
  actions: [N.actions.functionCall("ft_transfer", { receiver_id: "ada-shop.testnet", amount: "50000" }, 30000000000000n, 1n)],
  maxBlockHeight: 277045412n, nonce: 123456789012345n, publicKey: kp.getPublicKey(), receiverId: USDC, senderId: "buyer-1.testnet",
});
const encDA = N.encodeDelegateAction(da);
const signer = N.KeyPairSigner.fromSecretKey(buyerSecret);
const { delegateHash: hash, signedDelegate: signedDelegateAction } = await signer.signDelegateAction(da);
const encSD = N.encodeSignedDelegate(signedDelegateAction);
const rkp = N.KeyPair.fromString(relayerSecret);
const tx = N.createTransaction("relayer-1.testnet", rkp.getPublicKey(), "buyer-1.testnet", 99n,
  [N.actions.signedDelegate(signedDelegateAction)], new Uint8Array(32).fill(7));
const encTx = N.encodeTransaction(tx);
const rsigner = N.KeyPairSigner.fromSecretKey(relayerSecret);
const out = await rsigner.signTransaction(tx);
const signedTx = Array.isArray(out) ? out[1] : (out.signedTransaction ?? out);
const encSigned = N.encodeTransaction(signedTx);
const b64 = (u) => Buffer.from(u).toString("base64");
console.log(JSON.stringify({
  buyer_public_key: kp.getPublicKey().toString(),
  delegate_action_b64: b64(encDA),
  nep461_hash_hex: Buffer.from(hash).toString("hex"),
  signed_delegate_b64: b64(encSD),
  tx_b64: b64(encTx),
  tx_hash_b58: N.baseEncode(createHash("sha256").update(encTx).digest()),
  signed_tx_b64: b64(encSigned),
}, null, 1));
