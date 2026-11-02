#!/usr/bin/env python
"""
Benchmark: DuckDB thread and memory containment.

Each configuration runs in a SEPARATE CHILD PROCESS so VmPeak is
measured independently. This accurately reflects the production pattern
where the worker process accumulates peak memory from DuckDB queries.

Usage:
    uv run python benchmarks/bench_duckdb_containment.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

NUM_FILES = 10
ROWS_PER_FILE = 50_000


def get_host_cores() -> int:
    return os.cpu_count() or 1


def create_test_parquet_files(tmpdir: str) -> list[str]:
    """Create realistic Parquet files matching the span schema."""
    import arro3.core as ac
    import arro3.io as aio

    paths = []
    for f_idx in range(NUM_FILES):
        n = ROWS_PER_FILE
        batch = ac.RecordBatch.from_arrays(
            [
                ac.Array([1] * n, type=ac.DataType.int32()),
                ac.Array([f_idx % 10 + 1] * n, type=ac.DataType.int32()),
                ac.Array([f"/api/v1/ep-{i % 200}/" for i in range(n)], type=ac.DataType.utf8()),
                ac.Array([f"span-{i:012d}" for i in range(n)], type=ac.DataType.utf8()),
                ac.Array([f"txn-{i % 5000:012d}" for i in range(n)], type=ac.DataType.utf8()),
                ac.Array([["db", "http.client", "cache.get", "render"][i % 4] for i in range(n)], type=ac.DataType.utf8()),
                ac.Array([f"SELECT * FROM t_{i % 100} WHERE id={i}" for i in range(n)], type=ac.DataType.utf8()),
                ac.Array([float(i % 1000) + 0.5 for i in range(n)], type=ac.DataType.float64()),
                ac.Array([1700000000_000_000 + i * 1_000_000 for i in range(n)], type=ac.DataType.int64()).cast(ac.DataType.timestamp("us")),
            ],
            names=["organization_id", "project_id", "transaction_name", "span_id",
                   "transaction_id", "op", "description", "duration", "timestamp"],
        )
        path = os.path.join(tmpdir, f"spans_{f_idx:03d}.parquet")
        aio.write_parquet(batch, path, compression="zstd(3)")
        paths.append(path)
        del batch

    total_mb = sum(os.path.getsize(p) for p in paths) / (1024 * 1024)
    print(f"Created {NUM_FILES} files × {ROWS_PER_FILE:,} rows = {NUM_FILES * ROWS_PER_FILE:,} total rows ({total_mb:.1f} MB)")
    return paths


# ---------------------------------------------------------------------------
# Child process entry point — runs one config, reports JSON
# ---------------------------------------------------------------------------

CHILD_SCRIPT = r'''
import duckdb, json, os, resource, sys, time

def get_rss_mb():
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * resource.getpagesize() / (1024*1024)
    except: return 0.0

def get_vmpeak_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmPeak:"):
                    return int(line.split()[1]) / 1024
    except: pass
    return 0.0

paths_str, threads_str, mem_limit, spill_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
paths = json.loads(paths_str)

rss_before = get_rss_mb()
vmpeak_before = get_vmpeak_mb()

conn = duckdb.connect()
if mem_limit != "none":
    conn.execute(f"SET memory_limit = '{mem_limit}'")
if threads_str != "none":
    conn.execute(f"SET threads = {threads_str}")
conn.execute(f"SET temp_directory = '{spill_dir}'")

actual_threads = conn.execute("SELECT current_setting('threads')").fetchone()[0]

paths_list = ", ".join(f"'{p}'" for p in paths)
t0 = time.monotonic()

# Q1: Span group aggregation
conn.execute(f"""
    SELECT op, description, COUNT(*) as cnt, AVG(duration) as avg_dur,
           SUM(duration) as total_time
    FROM read_parquet([{paths_list}])
    GROUP BY op, description
    ORDER BY total_time DESC
    LIMIT 50
""").fetchall()

# Q2: N+1 detection
conn.execute(f"""
    SELECT transaction_name, op, description,
           COUNT(*) as total_spans,
           COUNT(DISTINCT transaction_id) as txn_count
    FROM read_parquet([{paths_list}])
    GROUP BY transaction_name, op, description
    HAVING COUNT(*) > 100
    ORDER BY total_spans DESC
    LIMIT 20
""").fetchall()

# Q3: Transaction trend
conn.execute(f"""
    SELECT DATE_TRUNC('hour', timestamp) as hour,
           COUNT(*) as cnt, AVG(duration) as avg_dur
    FROM read_parquet([{paths_list}])
    WHERE transaction_name = '/api/v1/ep-42/'
    GROUP BY hour ORDER BY hour
""").fetchall()

# Q4: Full scan with ORDER BY (worst case)
conn.execute(f"""
    SELECT span_id, transaction_id, op, description, duration, timestamp
    FROM read_parquet([{paths_list}])
    WHERE duration > 500
    ORDER BY duration DESC
    LIMIT 100
""").fetchall()

elapsed = time.monotonic() - t0
rss_after = get_rss_mb()
vmpeak_after = get_vmpeak_mb()
conn.close()

result = {
    "threads": actual_threads,
    "mem_limit": mem_limit,
    "rss_before": round(rss_before, 1),
    "rss_after": round(rss_after, 1),
    "rss_delta": round(rss_after - rss_before, 1),
    "vmpeak": round(vmpeak_after, 1),
    "vmpeak_delta": round(vmpeak_after - vmpeak_before, 1),
    "elapsed": round(elapsed, 3),
}
print(json.dumps(result))
'''


def run_config(paths: list[str], threads: int | None, mem_limit: str | None, spill_dir: str) -> dict:
    """Run one config in a fresh child process for clean VmPeak."""
    result = subprocess.run(
        [
            sys.executable, "-c", CHILD_SCRIPT,
            json.dumps(paths),
            str(threads) if threads is not None else "none",
            mem_limit or "none",
            spill_dir,
        ],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"  FAILED: {result.stderr[:200]}", file=sys.stderr)
        return {"error": result.stderr[:200]}
    return json.loads(result.stdout.strip())


def main():
    print(f"Host cores: {get_host_cores()}")
    print(f"Python: {sys.version.split()[0]}")
    print()

    tmpdir = tempfile.mkdtemp(prefix="duckdb_bench_")
    spill_dir = os.path.join(tmpdir, "spill")
    os.makedirs(spill_dir)

    try:
        paths = create_test_parquet_files(tmpdir)
        print()

        # --- Thread count benchmark ---
        configs = [
            ("default (host cores)", None, "256MB"),
            ("threads=8", 8, "256MB"),
            ("threads=4", 4, "256MB"),
            ("threads=2", 2, "256MB"),
            ("threads=1", 1, "256MB"),
        ]

        print(f"{'='*80}")
        print("  THREAD COUNT IMPACT (memory_limit=256MB)")
        print(f"{'='*80}")
        print(f"  {'Config':<22} {'Threads':>8} {'RSS Δ':>8} {'VmPeak':>10} {'VmPk Δ':>10} {'Time':>8}")
        print("-" * 80)

        for label, threads, mem in configs:
            r = run_config(paths, threads, mem, spill_dir)
            if "error" in r:
                continue
            print(
                f"  {label:<22} {r['threads']:>8} {r['rss_delta']:>+7.1f}M "
                f"{r['vmpeak']:>9.1f}M {r['vmpeak_delta']:>+9.1f}M "
                f"{r['elapsed']:>7.3f}s"
            )

        print()

        # --- Memory limit benchmark ---
        mem_configs = [
            ("no limit", 2, None),
            ("1024MB", 2, "1024MB"),
            ("512MB", 2, "512MB"),
            ("256MB", 2, "256MB"),
            ("128MB", 2, "128MB"),
        ]

        print(f"{'='*80}")
        print("  MEMORY LIMIT IMPACT (threads=2)")
        print(f"{'='*80}")
        print(f"  {'Config':<22} {'RSS Δ':>8} {'VmPeak':>10} {'VmPk Δ':>10} {'Time':>8}")
        print("-" * 80)

        for label, threads, mem in mem_configs:
            r = run_config(paths, threads, mem, spill_dir)
            if "error" in r:
                continue
            print(
                f"  {label:<22} {r['rss_delta']:>+7.1f}M "
                f"{r['vmpeak']:>9.1f}M {r['vmpeak_delta']:>+9.1f}M "
                f"{r['elapsed']:>7.3f}s"
            )

        print()

        # --- Before vs After ---
        print(f"{'='*80}")
        print("  BEFORE vs AFTER (production simulation)")
        print(f"{'='*80}")

        before = run_config(paths, None, "1024MB", spill_dir)
        after = run_config(paths, 2, "256MB", spill_dir)

        if "error" not in before and "error" not in after:
            print(f"  BEFORE (threads={before['threads']}, memory=1024MB):")
            print(f"    RSS delta: {before['rss_delta']:+.1f} MB")
            print(f"    VmPeak:    {before['vmpeak']:.1f} MB (delta: {before['vmpeak_delta']:+.1f} MB)")
            print(f"    Time:      {before['elapsed']:.3f}s")
            print()
            print(f"  AFTER  (threads={after['threads']}, memory=256MB):")
            print(f"    RSS delta: {after['rss_delta']:+.1f} MB")
            print(f"    VmPeak:    {after['vmpeak']:.1f} MB (delta: {after['vmpeak_delta']:+.1f} MB)")
            print(f"    Time:      {after['elapsed']:.3f}s")
            print()

            vmpeak_saved = before['vmpeak'] - after['vmpeak']
            rss_saved = before['rss_delta'] - after['rss_delta']
            time_diff = (after['elapsed'] / before['elapsed'] - 1) * 100 if before['elapsed'] > 0 else 0

            print(f"  VmPeak reduction: {vmpeak_saved:+.1f} MB")
            print(f"  RSS reduction:    {rss_saved:+.1f} MB")
            print(f"  Query time:       {time_diff:+.0f}%")

    finally:
        shutil.rmtree(tmpdir)


if __name__ == "__main__":
    main()
