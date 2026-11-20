# Scheme: `exact` on `bip122` (chains of unspent outputs)

Status: draft, not submitted. A working implementation: `polarka_agent/x402.py` (Python, standard library),
used with TrueNorth (NORTH), a chain of the Bitcoin kind.

## Summary

`exact` on chains of the Bitcoin kind (CAIP-2 namespace `bip122`) asks the client for a signed transaction with
exactly one output of at least the required amount to `payTo`. The facilitator checks it, hands it to its node
and, when the seller asks for it, waits until the transaction is in a block before it reports success.

Such chains have no transfer authorization that the payee executes later (as EIP-3009 does), and a waiting
transaction can be replaced by another one that pays more fee. So a seller that gives anything of value must
wait for a block. Waiting makes the transaction public while it waits, so this scheme adds a claim: a secret the
client puts in its payload, which binds what was bought to whoever made the payment.

## Scheme name

`exact`

## Network

CAIP-2 with the CAIP-4 namespace `bip122`: `bip122:` followed by the first 32 hexadecimal characters of the hash
of the chain's first block. Examples: Bitcoin `bip122:000000000019d6689c085ae165831e93`, TrueNorth
`bip122:470c8b8ae824c06575cfae0851282c49`.

## PaymentRequirements

```json
{
  "scheme": "exact",
  "network": "bip122:470c8b8ae824c06575cfae0851282c49",
  "amount": "50000000",
  "asset": "NORTH",
  "payTo": "north1z...",
  "maxTimeoutSeconds": 60,
  "extra": {"confirmations": 1}
}
```

- `amount`: base units of the chain's coin (for Bitcoin and NORTH, 10^-8), as a decimal string.
- `asset`: the chain's own coin. These chains have no token contracts; see open question 1.
- `payTo`: an address of the chain, in the form its software prints.
- `extra.confirmations`: blocks the transaction must be in before the seller answers. `0` means at once, when the
  facilitator's node takes it. Absent means `1`.

## PaymentPayload `payload`

```json
{"transaction": "<signed transaction, hexadecimal>", "claim": "<32 to 128 lowercase hexadecimal characters>"}
```

- `transaction`: fully signed, ready for the network. It must have exactly one output to `payTo`, and that output
  must be at least `amount`. Other outputs (change) are the client's business.
- `claim`: chosen at random by the client for this payment and shown to nobody but the seller. Required when
  `extra.confirmations` is above 0.

## Verification (normative)

The facilitator MUST refuse a payload when:

1. `x402Version` is not 2, or `accepted` differs from the requirements in `scheme`, `network`, `amount`,
   `asset` or `payTo` (`invalid_x402_version`, `invalid_payment_requirements`);
2. `transaction` is not hexadecimal, is longer than the facilitator allows, or does not decode
   (`invalid_payload`, `invalid_transaction`);
3. it has no output to `payTo`, or the one it has is below `amount` (`insufficient_funds`), or it has more than
   one (`invalid_payload`);
4. `claim` is missing while confirmations are required (`claim_required`), or is not of the stated form;
5. its node would not take the transaction (`testmempoolaccept` or the like), unless the node has it already
   (`transaction_refused_by_node`);
6. the transaction was taken before, and the claim shown is not the one it was taken with
   (`transaction_already_used`).

## Settlement

1. The first time a transaction is seen and passes, the facilitator writes down its id, the hash of its claim, the
   output that pays and the requirements, durably, before anything else. Then it hands the transaction to its node.
2. While the output is in fewer blocks than required, `/settle` answers `success: false`,
   `errorReason: "awaiting_confirmation"`, with `confirmations` and `required`. The resource server answers the
   client `202 Accepted` with `Retry-After` and `PAYMENT-RESPONSE`.
3. The client sends the same `PAYMENT-SIGNATURE` again. It is held to the requirements written down at step 1, not
   to the price of the moment.
4. Once deep enough, `/settle` answers `success: true` with `transaction` set to the transaction id, and the
   resource is given. The facilitator writes down that it was given.
5. If the node no longer knows the output (it was replaced), the facilitator gives the transaction to the node once
   more; if that fails, `payment_not_found`.
6. The same payment with the same claim within a short time after step 4 (600 seconds in the implementation) gets
   success again, so that an answer lost on the way can be fetched; after that, `transaction_already_used`.

## Security considerations

- **Replacement before a block.** Nodes built on Bitcoin Core 28 and later replace a waiting transaction with one
  that spends the same coins for more fee, whatever the first one signalled. With `confirmations: 0` the seller
  takes that risk; it fits prices meant to keep automated callers away, not payment for value.
- **The payment is public while it waits.** The claim is why a copied transaction buys nothing.
- **Selling twice.** The written record of step 1 comes before the transaction leaves the facilitator.
- **Reorganization.** One block is the default; a seller of something dear sets more.
- **The client's change.** The facilitator learns the client's change output; clients should use a fresh change
  address for each payment.
- **Fee.** The client pays the fee. A facilitator sponsors nothing, signs nothing and holds no coins.

## Open questions for the maintainers

1. `asset` for a chain's own coin: its ticker as here, or a fixed word such as `native`.
2. `202` and `awaiting_confirmation` are not in the HTTP transport yet. Is this the way the maintainers want
   payments that need a block, or should it be a scheme of its own?
3. `claim` as a field of the payload, or as an extension shared with other schemes that settle later.
