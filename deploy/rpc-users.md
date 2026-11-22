# Own RPC users for every service on the node

The node's cookie allows every call, `stop` and `invalidateblock` among them. The x402 facilitator, the
stamps service, the pool and both wallet servers (`deploy/walletd`) need a few calls each, so they get users of
their own that the node allows only those calls, and none of them is in the group that can read the cookie
(review 8, L5; the pool and its wallet server since 21 November 2026). The facilitator for sellers from outside
(`x402/polarka-x402-public.service`, from 0.2.3) makes the same calls as the gate's own, under a Unix user and an
RPC user of its own, `x402pub` (review 10, I3). From 0.2.3 the facilitators also read blocks (getblock,
getblockcount): a payment whose coin the seller spent before the buyer came back is found in its block (human
review, 22 November); each block is read once, as it comes (review 10, H2). Without them it is called lost, as before.

In the node's `truenorth.conf` (make the six `rpcauth` lines with `share/rpcauth/rpcauth.py` from the
TrueNorth sources, or with the same HMAC-SHA256 in a few lines of Python):

    rpcauth=x402:<salt>$<hmac>
    rpcauth=x402pub:<salt>$<hmac>
    rpcauth=stamps:<salt>$<hmac>
    rpcauth=walletpub:<salt>$<hmac>
    rpcauth=pool:<salt>$<hmac>
    rpcauth=walletd:<salt>$<hmac>
    rpcwhitelist=x402:decoderawtransaction,getblock,getblockcount,getblockhash,getmempoolentry,gettxout,sendrawtransaction,testmempoolaccept
    rpcwhitelist=x402pub:decoderawtransaction,getblock,getblockcount,getblockhash,getmempoolentry,gettxout,sendrawtransaction,testmempoolaccept
    rpcwhitelist=stamps:getblock,getblockcount,getblockhash,getblockheader,getmempoolentry,getrawtransaction,gettxoutproof
    rpcwhitelist=walletpub:estimatesmartfee,getblock,getblockchaininfo,getblockheader,getconnectioncount,getmempoolinfo,getnetworkinfo,getrawmempool,getrawtransaction,sendrawtransaction,testmempoolaccept,validateaddress
    rpcwhitelist=pool:getbestblockhash,getblock,getblockcount,getblockhash,getblocktemplate,gettxout,submitblock
    rpcwhitelist=walletd:estimatesmartfee,getblock,getblockchaininfo,getblockheader,getconnectioncount,getmempoolinfo,getnetworkinfo,getrawmempool,getrawtransaction,sendrawtransaction,testmempoolaccept,validateaddress
    rpcwhitelistdefault=0

`rpcwhitelistdefault=0` keeps the cookie as it was; without it the cookie would be allowed nothing. Each
service reads `user:password` from a file only it can read, given as `--rpc-cookie` (walletd: `--cookie`):

    install -d -m 0755 /etc/polarka-rpc
    printf 'x402:%s' "$PASSWORD" > /etc/polarka-rpc/x402 && chown x402:x402 /etc/polarka-rpc/x402 && chmod 0400 /etc/polarka-rpc/x402
    # the same for x402pub (another password), stamps, walletpub, pool and walletd; then restart the node, and the services
    gpasswd -d pool truenorth; gpasswd -d walletd truenorth   # if they were in the cookie's group

A call outside the list is answered `403`. Check it: the user `x402` gets `getblockhash` and is refused
`getblockcount`.
