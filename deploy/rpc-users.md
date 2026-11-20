# Own RPC users for the facilitator and the stamps service

The node's cookie allows every call, `stop` and `invalidateblock` among them. The x402 facilitator and the
stamps service need a few calls each, so they get users of their own that the node allows only those calls,
and they are not in the group that can read the cookie. The wallet server and the pool keep the cookie.

In the node's `truenorth.conf` (make the two `rpcauth` lines with `share/rpcauth/rpcauth.py` from the
TrueNorth sources, or with the same HMAC-SHA256 in a few lines of Python):

    rpcauth=x402:<salt>$<hmac>
    rpcauth=stamps:<salt>$<hmac>
    rpcwhitelist=x402:decoderawtransaction,getblockhash,getmempoolentry,gettxout,sendrawtransaction,testmempoolaccept
    rpcwhitelist=stamps:getblock,getblockcount,getblockhash,getblockheader,getmempoolentry,getrawtransaction,gettxoutproof
    rpcwhitelistdefault=0

`rpcwhitelistdefault=0` keeps the cookie as it was; without it the cookie would be allowed nothing. Each
service reads `user:password` from a file only it can read, given as `--rpc-cookie`:

    install -d -m 0755 /etc/polarka-rpc
    printf 'x402:%s' "$PASSWORD" > /etc/polarka-rpc/x402 && chown x402:x402 /etc/polarka-rpc/x402 && chmod 0400 /etc/polarka-rpc/x402
    # the same for stamps; then restart the node, and the two services

A call outside the list is answered `403`. Check it: the user `x402` gets `getblockhash` and is refused
`getblockcount`.
