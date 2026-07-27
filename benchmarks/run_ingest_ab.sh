#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# A/B ingest benchmark: Python vs Rust envelope path
#
# Starts compose.ingest_ab.yml (two production-shaped webs from the same
# image — GLITCHTIP_RUST_INGEST off/on — plus postgres, valkey, and a
# load-generator container), then runs bench_ingest_ab.py inside the
# bench container. All arguments are forwarded to the Python driver.
#
# Usage:
#   bash benchmarks/run_ingest_ab.sh                       # full run
#   bash benchmarks/run_ingest_ab.sh --segments 2 -n 500   # quick pass
#   bash benchmarks/run_ingest_ab.sh --workloads prodmix
#
# The stack stays up between runs so iteration is fast. Tear it down with:
#   docker compose -f benchmarks/compose.ingest_ab.yml down -v
# ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE="docker compose -f $SCRIPT_DIR/compose.ingest_ab.yml"

cd "$REPO_ROOT"
echo ">>> Starting A/B stack..."
$COMPOSE up -d --build 2>&1 | tail -5
echo ""

# Both webs run migrations + partition bootstrap on first start; the driver
# itself waits on /_health/, so just make sure the bench container is up.
echo ">>> Waiting for bench container..."
for i in $(seq 1 60); do
    if $COMPOSE exec -T bench true 2>/dev/null; then
        break
    fi
    [ "$i" -eq 60 ] && { echo "ERROR: bench container not up"; exit 1; }
    sleep 1
done

STAMP=$(date +%Y%m%d_%H%M%S)
JSON_OUT="/code/benchmarks/ingest_ab_results/${STAMP}.json"
$COMPOSE exec -T bench mkdir -p /code/benchmarks/ingest_ab_results

echo ">>> Running benchmark (results: benchmarks/ingest_ab_results/${STAMP}.json)"
echo ""
$COMPOSE exec -T bench python benchmarks/bench_ingest_ab.py \
    --json-out "$JSON_OUT" "$@"

echo ""
echo ">>> Done. Stack left running for iteration; tear down with:"
echo "    docker compose -f benchmarks/compose.ingest_ab.yml down -v"
