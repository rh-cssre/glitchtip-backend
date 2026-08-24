#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Concurrency benchmark for the ingest + API read path under realistic
# conditions:
#
#   - real ASGI server (granian)
#   - DEBUG=false
#   - tc netem DB latency (default: 2ms — low enough to resemble LAN RTT,
#     high enough that the event loop has something to overlap)
#   - sizeable PG pool (default max_size=40 per worker)
#   - production malloc tuning (MALLOC_MMAP_THRESHOLD_=65536)
#
# Runs ``bench_ingest_memory.py --mode mixed``, samples cgroup memory
# every 0.5s, and prints a summary with throughput (req/s), error rate,
# and RSS peak.
#
# Usage:
#   bash benchmarks/run_concurrency_bench.sh [BENCH_ARGS...]
#
# Env:
#   DB_LATENCY_MS           One-way latency (default: 2)
#   DB_JITTER_MS            Jitter (default: 0)
#   DATABASE_POOL_MAX_SIZE  PG pool upper bound (default: 40)
# ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE="docker compose -f $SCRIPT_DIR/compose.bench.yml"

DB_LATENCY_MS="${DB_LATENCY_MS:-2}"
DB_JITTER_MS="${DB_JITTER_MS:-0}"
export DATABASE_POOL_MIN_SIZE="${DATABASE_POOL_MIN_SIZE:-5}"
export DATABASE_POOL_MAX_SIZE="${DATABASE_POOL_MAX_SIZE:-40}"
export MALLOC_MMAP_THRESHOLD_="${MALLOC_MMAP_THRESHOLD_:-65536}"
export MALLOC_TRIM_THRESHOLD_="${MALLOC_TRIM_THRESHOLD_:-65536}"
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-4}"

BENCH_ARGS=("$@")
RESULTS_DIR=$(mktemp -d /tmp/gt_concurrency_XXXXXX)

MONITOR_PID=0
cleanup() {
    [ "$MONITOR_PID" -ne 0 ] && kill "$MONITOR_PID" 2>/dev/null && wait "$MONITOR_PID" 2>/dev/null || true
    MONITOR_PID=0
}
trap cleanup EXIT

echo "============================================================"
echo "  CONCURRENCY BENCHMARK"
echo "  DB latency:  ${DB_LATENCY_MS}ms +/- ${DB_JITTER_MS}ms"
echo "  PG pool:     min=${DATABASE_POOL_MIN_SIZE}, max=${DATABASE_POOL_MAX_SIZE}"
echo "  Bench args:  ${BENCH_ARGS[*]:-<defaults>}"
echo "  Results:     $RESULTS_DIR"
echo "============================================================"

cd "$REPO_ROOT"

result_json="$RESULTS_DIR/result.json"
memlog="$RESULTS_DIR/memlog.csv"

echo ">>> Bringing down any existing stack"
$COMPOSE down -v 2>&1 | tail -3

echo ">>> Starting stack"
$COMPOSE up -d --build 2>&1 | tail -5

echo ">>> Waiting for web (up to 300s)"
for i in $(seq 1 300); do
    result=$($COMPOSE exec -T bench python -c "
import httpx
try:
    r = httpx.get('http://web:8000/_health/', timeout=3)
    print('ok' if r.status_code < 500 else 'fail')
except Exception as e:
    print(f'err')
" 2>&1 || echo "container_not_ready")
    if [ "$result" = "ok" ]; then
        echo "    Ready after ${i}s"
        break
    fi
    if [ "$i" -eq 300 ]; then
        echo "ERROR: web not ready; logs:"
        $COMPOSE logs web --tail 30
        exit 1
    fi
    sleep 1
done

echo ">>> Installing tc (if needed) and applying ${DB_LATENCY_MS}ms latency"
$COMPOSE exec -T web bash -c "which tc >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq iproute2 >/dev/null 2>&1)"
$COMPOSE exec -T web tc qdisc del dev eth0 root 2>/dev/null || true
POSTGRES_IP=$($COMPOSE exec -T web getent hosts postgres | awk '{print $1}')
VALKEY_IP=$($COMPOSE exec -T web getent hosts valkey | awk '{print $1}')
$COMPOSE exec -T web tc qdisc add dev eth0 root handle 1: prio priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
$COMPOSE exec -T web tc qdisc add dev eth0 parent 1:2 handle 20: netem delay ${DB_LATENCY_MS}ms ${DB_JITTER_MS}ms
$COMPOSE exec -T web tc filter add dev eth0 parent 1:0 protocol ip u32 match ip dst "$POSTGRES_IP"/32 flowid 1:2
$COMPOSE exec -T web tc filter add dev eth0 parent 1:0 protocol ip u32 match ip dst "$VALKEY_IP"/32 flowid 1:2

# Baseline RSS (post-startup, pre-bench)
WEB_CONTAINER=$($COMPOSE ps -q web)
rss_baseline_bytes=$(docker exec "$WEB_CONTAINER" cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)

echo "timestamp_s,mem_bytes" > "$memlog"
(
    while true; do
        mem=$(docker exec "$WEB_CONTAINER" cat /sys/fs/cgroup/memory.current 2>/dev/null || echo "0")
        echo "$(date +%s.%N),$mem" >> "$memlog"
        sleep 0.5
    done
) &
MONITOR_PID=$!

echo ">>> Running bench (${BENCH_ARGS[*]:-<defaults>})"
BENCH_CONTAINER=$($COMPOSE ps -q bench)
$COMPOSE exec -T bench python benchmarks/bench_ingest_memory.py \
    --mode mixed --json-out "/tmp/result.json" "${BENCH_ARGS[@]}" || true
# JSON is written inside the bench container; copy it out so the
# cleanup cycle (``down -v``) doesn't take it down with the stack.
docker cp "$BENCH_CONTAINER:/tmp/result.json" "$result_json" 2>/dev/null \
    || $COMPOSE exec -T bench cat "/tmp/result.json" > "$result_json" 2>/dev/null \
    || echo "{}" > "$result_json"

kill "$MONITOR_PID" 2>/dev/null && wait "$MONITOR_PID" 2>/dev/null || true
MONITOR_PID=0

rss_final_bytes=$(docker exec "$WEB_CONTAINER" cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)
# Peak across the run
rss_peak_bytes=$(awk -F',' 'NR>1 && $2>m{m=$2} END{print m+0}' "$memlog")

# Stash RSS metadata alongside the request stats
python3 - "$result_json" "$rss_baseline_bytes" "$rss_peak_bytes" "$rss_final_bytes" <<'PY'
import json, sys
path, b, p, f = sys.argv[1:5]
d = json.loads(open(path).read()) if open(path).read().strip() else {}
d["rss_baseline_mb"] = int(b) / 1024 / 1024
d["rss_peak_mb"] = int(p) / 1024 / 1024
d["rss_final_mb"] = int(f) / 1024 / 1024
open(path, "w").write(json.dumps(d, indent=2))
PY

# Summary
echo ""
echo "════════════════════ SUMMARY ════════════════════"
python3 - "$result_json" <<'PY'
import json
import sys

try:
    d = json.loads(open(sys.argv[1]).read())
except (OSError, json.JSONDecodeError):
    print("  no results")
    sys.exit(0)

base = d.get("rss_baseline_mb", 0.0)
peak = d.get("rss_peak_mb", 0.0)
overall = d.get("overall") or {}
print()
print(f"{'rss_base':>10} {'rss_peak':>10} {'rss_growth':>12}"
      f" {'throughput':>12} {'err_rate':>10} {'p50':>8} {'p95':>8} {'p99':>8}")
print("-" * 90)
print(f"{base:>7.1f}MB {peak:>7.1f}MB {peak-base:>+9.1f}MB "
      f"{overall.get('req_per_s', 0.0):>9.1f}/s {overall.get('error_rate', 0.0)*100:>7.1f}%  "
      f"{overall.get('p50_ms', 0.0):>6.1f}ms {overall.get('p95_ms', 0.0):>6.1f}ms "
      f"{overall.get('p99_ms', 0.0):>6.1f}ms")
print()
print(f"results: {sys.argv[1]}")
PY
