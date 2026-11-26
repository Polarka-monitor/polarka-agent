#!/bin/zsh
# Start a local fork of Base (anvil on 127.0.0.1:8545) and fund the test keys in it: 5 USDC for the payer, minted
# through USDC's masterMinter, and 0.01 ETH of gas. Nothing is ever sent to Base itself; the fork only reads it.
set -eu
B=${FOUNDRY_BIN:-$HOME/.foundry/bin}
PY=${PY:-python3}
R=http://127.0.0.1:8545
U=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
A=${KEYS:-$HOME/.config/usdc-fork-test}/addresses.json
P=$($PY -c "import json,sys; print(json.load(open(sys.argv[1]))['payer'])" $A)
G=$($PY -c "import json,sys; print(json.load(open(sys.argv[1]))['gas'])" $A)
nohup $B/anvil --fork-url https://mainnet.base.org --host 127.0.0.1 --port 8545 --auto-impersonate > ${RUN_DIR:-/tmp}/anvil-usdc-fork.log 2>&1 &
for i in {1..60}; do $B/cast chain-id --rpc-url $R >/dev/null 2>&1 && break; sleep 0.5; done
MM=$($B/cast call $U "masterMinter()(address)" --rpc-url $R)
$B/cast rpc anvil_setBalance $MM 0xDE0B6B3A7640000 --rpc-url $R >/dev/null
$B/cast send $U "configureMinter(address,uint256)" $MM 1000000000 --from $MM --unlocked --rpc-url $R >/dev/null
$B/cast send $U "mint(address,uint256)" $P 5000000 --from $MM --unlocked --rpc-url $R >/dev/null
$B/cast rpc anvil_setBalance $G 0x2386F26FC10000 --rpc-url $R >/dev/null
echo "fork of Base at block $($B/cast block-number --rpc-url $R); payer $P holds $($B/cast call $U 'balanceOf(address)(uint256)' $P --rpc-url $R)"
