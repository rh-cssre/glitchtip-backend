#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Memory Growth Benchmark with forced DB latency (tc netem)
#
# Starts a dedicated Docker Compose stack, injects network latency into
# the web container's connection to Postgres/Valkey, then fires high-
# concurrency traffic and samples cgroup memory every 0.5 s.
#
# Usage:
#   bash benchmarks/run_ingest_bench.sh [BENCH_ARGS...]
#
# Examples:
#   # Default ingest-only benchmark
#   bash benchmarks/run_ingest_bench.sh
#
#   # Mixed workload, 300 concurrency, 20 waves
#   bash benchmarks/run_ingest_bench.sh --mode mixed -c 300 -n 1500 -w 20
#
#   # Quick smoke test
#   bash benchmarks/run_ingest_bench.sh -c 50 -n 200 -w 2 --pause 1
#
# Environment variables:
#   DB_LATENCY_MS   Artificial DB round-trip latency (default: 100)
#   DB_JITTER_MS    Latency jitter (default: 10)
# ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE="docker compose -f $SCRIPT_DIR/compose.bench.yml"

DB_LATENCY_MS="${DB_LATENCY_MS:-100}"
DB_JITTER_MS="${DB_JITTER_MS:-10}"

# All remaining args are forwarded to the Python benchmark script
BENCH_ARGS=("$@")

MONITOR_PID=0
MEMLOG=""

cleanup() {
    [ "$MONITOR_PID" -ne 0 ] && kill "$MONITOR_PID" 2>/dev/null && wait "$MONITOR_PID" 2>/dev/null || true
    MONITOR_PID=0
    $COMPOSE exec -T web tc qdisc del dev eth0 root 2>/dev/null || true
}
trap cleanup EXIT

echo "============================================================"
echo "  MEMORY GROWTH BENCHMARK"
echo "  DB Latency:  ${DB_LATENCY_MS}ms +/- ${DB_JITTER_MS}ms (tc netem)"
echo "  Bench args:  ${BENCH_ARGS[*]:-<defaults>}"
echo "============================================================"
echo ""

# ── 1. Start the stack (reuses existing containers if possible) ─────
cd "$REPO_ROOT"
echo ">>> Starting bench stack..."
$COMPOSE up -d --build 2>&1 | tail -5
echo ""

# ── 2. Wait for containers, then for the web server ─────────────────
echo ">>> Waiting for containers to start..."
sleep 3  # Let docker stabilize after recreate

echo ">>> Waiting for web server..."
for i in $(seq 1 300); do
    result=$($COMPOSE exec -T bench python -c "
import httpx
try:
    r = httpx.get('http://web:8000/_health/', timeout=3)
    print('ok' if r.status_code < 500 else 'fail')
except Exception as e:
    print(f'err:{e}')
" 2>&1 || echo "container_not_ready")
    if [ "$result" = "ok" ]; then
        echo "    Ready after ${i}s"
        break
    fi
    if [ "$i" -eq 300 ]; then
        echo "ERROR: Web server not ready in 300s (last check: $result). Logs:"
        $COMPOSE logs web --tail 20
        exit 1
    fi
    sleep 1
done
echo ""

# ── 3. Baseline memory ──────────────────────────────────────────────
echo ">>> Baseline memory:"
docker stats --no-stream --format '  {{.Name}}\t{{.MemUsage}}' 2>&1 | grep web || true
echo ""

# ── 4. Apply tc netem latency ────────────────────────────────────────
echo ">>> Installing iproute2 in web container (if needed)..."
$COMPOSE exec -T web bash -c "which tc >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq iproute2 >/dev/null 2>&1)"

# Remove any leftover rules
$COMPOSE exec -T web tc qdisc del dev eth0 root 2>/dev/null || true

POSTGRES_IP=$($COMPOSE exec -T web getent hosts postgres | awk '{print $1}')
VALKEY_IP=$($COMPOSE exec -T web getent hosts valkey | awk '{print $1}')

$COMPOSE exec -T web tc qdisc add dev eth0 root handle 1: prio priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
$COMPOSE exec -T web tc qdisc add dev eth0 parent 1:2 handle 20: netem delay ${DB_LATENCY_MS}ms ${DB_JITTER_MS}ms
$COMPOSE exec -T web tc filter add dev eth0 parent 1:0 protocol ip u32 match ip dst "$POSTGRES_IP"/32 flowid 1:2
$COMPOSE exec -T web tc filter add dev eth0 parent 1:0 protocol ip u32 match ip dst "$VALKEY_IP"/32 flowid 1:2
echo "    Applied ${DB_LATENCY_MS}ms +/- ${DB_JITTER_MS}ms to postgres ($POSTGRES_IP) and valkey ($VALKEY_IP)"
echo ""

# ── 5. Start cgroup memory monitor ──────────────────────────────────
WEB_CONTAINER=$($COMPOSE ps -q web)
MEMLOG=$(mktemp /tmp/bench_memory_XXXXXX.csv)
echo "timestamp_s,mem_bytes" > "$MEMLOG"

(
    while true; do
        mem=$(docker exec "$WEB_CONTAINER" cat /sys/fs/cgroup/memory.current 2>/dev/null || echo "0")
        echo "$(date +%s.%N),$mem" >> "$MEMLOG"
        sleep 0.5
    done
) &
MONITOR_PID=$!

# ── 6. Run the benchmark ────────────────────────────────────────────
echo ">>> Running benchmark..."
echo ""

$COMPOSE exec -T bench python benchmarks/bench_ingest_memory.py "${BENCH_ARGS[@]}"

echo ""

# ── 7. Stop monitor, remove tc ──────────────────────────────────────
kill "$MONITOR_PID" 2>/dev/null && wait "$MONITOR_PID" 2>/dev/null || true
MONITOR_PID=0

echo ">>> Post-benchmark memory:"
docker stats --no-stream --format '  {{.Name}}\t{{.MemUsage}}' 2>&1 | grep web || true
echo ""

# ── 8. Memory timeline ──────────────────────────────────────────────
echo ">>> Memory timeline (sampled every ~2s):"
echo ""
echo "   Time (s) |  Memory (MB) | Delta (MB)"
echo "  ----------|--------------|----------"
awk -F',' '
NR == 1 { next }
NR == 2 { t0 = $1; base = $2 }
{
    if ((NR - 1) % 4 == 0) {
        printf "  %8.0f   | %10.1f   | %+.1f\n", $1 - t0, $2/1048576, ($2-base)/1048576
    }
}' "$MEMLOG"

echo ""
echo ">>> Summary:"
awk -F',' '
NR == 1 { next }
NR == 2 { mn = $2; mx = $2; base = $2 }
{
    if ($2 < mn) mn = $2
    if ($2 > mx) mx = $2
    count++; last = $2
}
END {
    printf "  Baseline: %8.1f MB\n", base/1048576
    printf "  Peak:     %8.1f MB\n", mx/1048576
    printf "  Final:    %8.1f MB\n", last/1048576
    printf "  Growth:   %8.1f MB (%+.0f%%)\n", (mx-base)/1048576, ((mx-base)/base)*100
}' "$MEMLOG"

echo ""
echo "  Full CSV: $MEMLOG"
echo "============================================================"
