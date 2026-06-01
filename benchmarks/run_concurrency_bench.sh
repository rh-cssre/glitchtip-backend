#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Concurrency benchmark for the async-backend cursor path under
# realistic conditions:
#
#   - real ASGI server (granian)
#   - DEBUG=false
#   - tc netem DB latency (default: 2ms — enough for asyncio to beat
#     sync, low enough to resemble LAN RTT)
#   - sizeable PG pool (default max_size=40 per worker)
#   - production malloc tuning (MALLOC_MMAP_THRESHOLD_=65536)
#
# Runs ``bench_ingest_memory.py --mode mixed`` against each backend,
# samples cgroup memory every 0.5s, and prints a summary with
# throughput (req/s), error rate, and RSS peak per backend.
#
# Usage:
#   bash benchmarks/run_concurrency_bench.sh [BENCH_ARGS...]
#
# Env:
#   DB_LATENCY_MS           One-way latency (default: 2)
#   DB_JITTER_MS            Jitter (default: 0)
#   DATABASE_POOL_MAX_SIZE  PG pool upper bound (default: 40)
#   BACKENDS                Backend variants to run (default: "async")
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

BACKENDS="${BACKENDS:-async}"
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
echo "  Backends:    $BACKENDS"
echo "  DB latency:  ${DB_LATENCY_MS}ms +/- ${DB_JITTER_MS}ms"
echo "  PG pool:     min=${DATABASE_POOL_MIN_SIZE}, max=${DATABASE_POOL_MAX_SIZE}"
echo "  Bench args:  ${BENCH_ARGS[*]:-<defaults>}"
echo "  Results:     $RESULTS_DIR"
echo "============================================================"

cd "$REPO_ROOT"

run_one() {
    local backend="$1"
    local result_json="$RESULTS_DIR/${backend}.json"
    local memlog="$RESULTS_DIR/${backend}.memlog.csv"

    echo ""
    echo "════════════════════ $backend ════════════════════"
    echo ">>> Bringing down any existing stack"
    BACKEND_VARIANT="$backend" $COMPOSE down -v 2>&1 | tail -3

    echo ">>> Starting stack with BACKEND_VARIANT=$backend"
    BACKEND_VARIANT="$backend" $COMPOSE up -d --build 2>&1 | tail -5

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
            return 1
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
        --mode mixed --json-out "/tmp/${backend}.json" "${BENCH_ARGS[@]}" || true
    # JSON is written inside the bench container; copy it out so the
    # cleanup cycle (``down -v``) doesn't take it down with the stack.
    docker cp "$BENCH_CONTAINER:/tmp/${backend}.json" "$result_json" 2>/dev/null \
        || $COMPOSE exec -T bench cat "/tmp/${backend}.json" > "$result_json" 2>/dev/null \
        || echo "{}" > "$result_json"

    kill "$MONITOR_PID" 2>/dev/null && wait "$MONITOR_PID" 2>/dev/null || true
    MONITOR_PID=0

    rss_final_bytes=$(docker exec "$WEB_CONTAINER" cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)
    # Peak across the run
    rss_peak_bytes=$(awk -F',' 'NR>1 && $2>m{m=$2} END{print m+0}' "$memlog")

    # Stash per-backend metadata
    python3 - "$result_json" "$rss_baseline_bytes" "$rss_peak_bytes" "$rss_final_bytes" <<'PY'
import json, sys
path, b, p, f = sys.argv[1:5]
d = json.loads(open(path).read()) if open(path).read().strip() else {}
d["rss_baseline_mb"] = int(b) / 1024 / 1024
d["rss_peak_mb"] = int(p) / 1024 / 1024
d["rss_final_mb"] = int(f) / 1024 / 1024
open(path, "w").write(json.dumps(d, indent=2))
PY
}

for backend in $BACKENDS; do
    run_one "$backend"
done

# Final comparison
echo ""
echo "════════════════════ COMPARISON ════════════════════"
python3 - "$RESULTS_DIR" $BACKENDS <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
backends = sys.argv[2:]
loaded = {}
for b in backends:
    p = root / f"{b}.json"
    if not p.exists():
        continue
    try:
        loaded[b] = json.loads(p.read_text())
    except json.JSONDecodeError:
        print(f"  {b}: JSON parse error")
        continue

print()
print(f"{'backend':<10} {'rss_base':>10} {'rss_peak':>10} {'rss_growth':>12}"
      f" {'throughput':>12} {'err_rate':>10} {'p50':>8} {'p95':>8} {'p99':>8}")
print("-" * 100)
for b in backends:
    d = loaded.get(b, {})
    base = d.get("rss_baseline_mb", 0.0)
    peak = d.get("rss_peak_mb", 0.0)
    # Extract overall throughput/latency from the per-type mixed results
    overall = d.get("overall") or {}
    throughput = overall.get("req_per_s", 0.0)
    err = overall.get("error_rate", 0.0)
    p50 = overall.get("p50_ms", 0.0)
    p95 = overall.get("p95_ms", 0.0)
    p99 = overall.get("p99_ms", 0.0)
    print(f"{b:<10} {base:>7.1f}MB {peak:>7.1f}MB {peak-base:>+9.1f}MB "
          f"{throughput:>9.1f}/s {err*100:>7.1f}%  {p50:>6.1f}ms {p95:>6.1f}ms {p99:>6.1f}ms")
print()
print(f"results: {root}/")
PY
