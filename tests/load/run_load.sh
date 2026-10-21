#!/bin/bash
# SIPp load runner for the ivr-lab stack.
#
# Usage: ./run_load.sh <extension> <max_concurrent> <call_rate_per_sec> <duration_ms> <total_calls> [label]
#
#   extension        9000 = IVR path (DTMF timeout -> no_choice)
#                    9001 = straight to sales queue (MOH + mixing bridge, caller drops after duration)
#                    9002 = straight to support queue
#   max_concurrent   sipp -l: hard cap on simultaneous calls
#   call_rate_per_sec  sipp -r <n> -rp 1000: attempts per second (concurrency ~= rate * call_lifetime)
#   duration_ms      sipp -d: max call length before sipp sends BYE
#   total_calls      sipp -m: stop after this many attempted calls
#
# Examples:
#   ./run_load.sh 9000 10   5  40000 200   ivr-10
#   ./run_load.sh 9000 100  20 40000 2000  ivr-100
#   ./run_load.sh 9000 500  25 40000 8000  ivr-500
#   ./run_load.sh 9001 500  25 60000 8000  queue-500
set -euo pipefail

EXT=${1:?extension}
CONC=${2:?max concurrent}
RATE=${3:?calls/sec}
DUR=${4:?duration ms}
TOTAL=${5:?total calls}
LABEL=${6:-load-${EXT}-${CONC}}
OUT="$(dirname "$0")/results"
mkdir -p "$OUT"

echo "[$LABEL] starting: ext=$EXT concurrency<=$CONC rate=${RATE}/s dur=${DUR}ms total=$TOTAL"
echo "[$LABEL] sampling docker stats to $OUT/$LABEL.stats ..."

# sample container resource usage for the whole run.
# Set SSH_TARGET (e.g. iszzy@192.168.1.182) to sample a remote docker host.
stats_line() {
  if [ -n "${SSH_TARGET:-}" ]; then
    # NB: the remote command must be ONE quoted arg — ssh concatenates argv
    # and quotes would not survive the trip.
    ssh "$SSH_TARGET" 'docker stats --no-stream --format "{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}" asterisk cc-backend cc-postgres cc-redis' 2>/dev/null
  else
    docker stats --no-stream --format \
      '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}' asterisk cc-backend cc-postgres cc-redis 2>/dev/null
  fi
}
while true; do
  echo "$(date +%s) $(stats_line | tr '\n' ' ')"
  sleep 5
done > "$OUT/$LABEL.stats" &
STATS_PID=$!

SCEN=$(mktemp /tmp/uac-XXXX.xml)
sed "s/__DURATION__/$DUR/g" "$(dirname "$0")/uac.xml" > "$SCEN"

docker run --rm --network cc-net --name "sipp-$LABEL" \
  -v "$SCEN:/scenarios/run.xml:ro" \
  ivr-lab-sipp \
  -sf /scenarios/run.xml \
  -s "$EXT" \
  -l "$CONC" \
  -r "$RATE" -rp 1000 \
  -m "$TOTAL" \
  "${SIPP_TARGET:-192.168.1.182}:5060" \
  2>&1 | tee "$OUT/$LABEL.log" || true
rm -f "$SCEN"

kill $STATS_PID 2>/dev/null || true
echo "[$LABEL] done. log: $OUT/$LABEL.log stats: $OUT/$LABEL.stats"
