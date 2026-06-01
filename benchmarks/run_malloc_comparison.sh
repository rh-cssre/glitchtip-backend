#!/usr/bin/env bash
set -euo pipefail

# Compare malloc tuning strategies for memory growth.
# Runs the mixed workload benchmark with different malloc env vars.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE="docker compose -f $SCRIPT_DIR/compose.bench.yml"
BENCH_ARGS="--mode mixed -c 500 -n 2000 -w 15 --pause 2"

RESULTS_FILE="/tmp/malloc_comparison_$(date +%Y%m%d_%H%M%S).txt"

run_variant() {
    local label="$1"
    shift
    local env_args=("$@")

    echo "============================================================"
    echo "  VARIANT: $label"
    echo "  ENV: ${env_args[*]}"
    echo "============================================================"

    # Stop previous run
    $COMPOSE down 2>/dev/null || true

    # Run benchmark with the given env vars
    env "${env_args[@]}" bash "$SCRIPT_DIR/run_ingest_bench.sh" $BENCH_ARGS 2>&1 | \
        tee /tmp/bench_current.txt | \
        grep -E "^(>>>|  Baseline|  Peak|  Final|  Growth|  )"

    # Extract summary
    baseline=$(grep "Baseline:" /tmp/bench_current.txt | awk '{print $2}')
    peak=$(grep "Peak:" /tmp/bench_current.txt | awk '{print $2}')
    final=$(grep "Final:" /tmp/bench_current.txt | awk '{print $2}')
    growth=$(grep "Growth:" /tmp/bench_current.txt | awk '{print $2}')
    growth_pct=$(grep "Growth:" /tmp/bench_current.txt | awk '{print $4}')

    echo "$label | $baseline | $peak | $final | $growth | $growth_pct" >> "$RESULTS_FILE"
    echo ""
}

echo "Variant | Baseline | Peak | Final | Growth | %" > "$RESULTS_FILE"
echo "--------|----------|------|-------|--------|--" >> "$RESULTS_FILE"

run_variant "1. Baseline (ARENA=4)" \
    MALLOC_ARENA_MAX=4 MALLOC_MMAP_THRESHOLD_= MALLOC_TRIM_THRESHOLD_=

run_variant "2. ARENA=2" \
    MALLOC_ARENA_MAX=2 MALLOC_MMAP_THRESHOLD_= MALLOC_TRIM_THRESHOLD_=

run_variant "3. MMAP+TRIM=64K" \
    MALLOC_ARENA_MAX=4 MALLOC_MMAP_THRESHOLD_=65536 MALLOC_TRIM_THRESHOLD_=65536

run_variant "4. ARENA=2+MMAP+TRIM" \
    MALLOC_ARENA_MAX=2 MALLOC_MMAP_THRESHOLD_=65536 MALLOC_TRIM_THRESHOLD_=65536

# Clean up
$COMPOSE down 2>/dev/null || true

echo ""
echo "============================================================"
echo "  COMPARISON RESULTS"
echo "============================================================"
cat "$RESULTS_FILE"
echo ""
echo "Full results: $RESULTS_FILE"
