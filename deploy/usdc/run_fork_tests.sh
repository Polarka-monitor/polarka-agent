#!/bin/zsh
# All fork tests of the USDC facilitator and the gate: a fresh local fork of Base (fork_start.sh), the facilitator
# with a fresh journal, then the self-test with the attacks, a late block, a facilitator killed while it waits, and
# the gate end to end with a standard x402 buyer. Nothing is sent to Base itself. Full outputs stay in the run folder.
set -u
D=${0:A:h}
PY=${PY:-python3}                       # a Python with x402[evm]==2.29.0
B=${FOUNDRY_BIN:-$HOME/.foundry/bin}    # anvil and cast (Foundry 1.10.0)
K=${KEYS:-$HOME/.config/usdc-fork-test}  # test keys from gen_keys.py
TO=${PAY_TO:-0x522475540CA835D14E287a645d22c4B367703dac}   # any address: only a fork
RUN=${RUN_DIR:-/tmp}/usdc-fork-$(date +%Y%m%d-%H%M%S)
mkdir -p $RUN
start_fac() {   # $1: seconds the facilitator waits for a block
  (cd $D && nohup $PY usdc_facilitator.py --network eip155:8453 --rpc http://127.0.0.1:8545 --key-file $K/gas.key \
    --pay-to $TO --port 8402 --confirm-timeout $1 --expiry-margin 5 --lost-after 20 --state-dir $RUN/state >> $RUN/facilitator.log 2>&1 &)
  for i in {1..40}; do curl -s -m 2 http://127.0.0.1:8402/health >/dev/null && return 0; sleep 0.5; done
  echo "facilitator did not start"; tail -5 $RUN/facilitator.log; return 1
}
stop_fac() {    # $1 (optional): a signal such as -9
  if [[ -n ${1:-} ]]; then pkill $1 -f "usdc_facilitator.py --network eip155:8453"
  else pkill -f "usdc_facilitator.py --network eip155:8453"; fi
  sleep 1
}
pkill -f "anvil --fork-url" ; stop_fac
zsh $D/fork_start.sh || exit 1
start_fac 5 || exit 1
cd $D
echo "=== self-test with settlement, attacks, payments at once, the second and the third reading (29)"
$PY usdc_selftest.py --network eip155:8453 --rpc http://127.0.0.1:8545 --payer-key $K/payer.key --pay-to $TO --settle --fork 2>&1 | tee $RUN/selftest.log | grep -E "^(OK|FAIL)|checks passed" | cut -c1-160
echo "=== late block: send, restart, finish (4)"
$PY usdc_pendingtest.py send --payer-key $K/payer.key --pay-to $TO --save $RUN/late.json 2>&1 | grep -E "^(OK|FAIL)" | cut -c1-200
stop_fac; start_fac 5 || exit 1
$PY usdc_pendingtest.py finish --payer-key $K/payer.key --pay-to $TO --save $RUN/late.json 2>&1 | grep -E "^(OK|FAIL)|checks passed" | cut -c1-200
echo "=== killed while it waits for the block (L5a)"
stop_fac; start_fac 30 || exit 1
$PY usdc_pendingtest.py send --leave --payer-key $K/payer.key --pay-to $TO --save $RUN/killed.json 2>&1 | grep -E "^(OK|FAIL)" | cut -c1-200
stop_fac -9; start_fac 5 || exit 1
$PY usdc_pendingtest.py finish --payer-key $K/payer.key --pay-to $TO --save $RUN/killed.json 2>&1 | grep -E "^(OK|FAIL)|checks passed" | cut -c1-200
echo "=== the gate end to end with a standard x402 buyer (8)"
$PY usdc_gate_e2e.py 2>&1 | tee $RUN/gate_e2e.log | grep -E "^(OK|FAIL)|checks passed" | cut -c1-200
echo "=== the facilitator's log (warnings)"
grep -E "WARNING|ALARM|Traceback" $RUN/facilitator.log | cut -c1-160 | tail -12
stop_fac; pkill -f "anvil --fork-url"; echo "stopped; run folder $RUN"
