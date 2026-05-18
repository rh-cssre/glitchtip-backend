#!/usr/bin/env python
"""
Benchmark: Memory growth under high concurrency + artificial DB latency.

Supports two modes:
  --mode ingest    Hammer only the event envelope endpoint (default).
  --mode mixed     Realistic mixed workload: ingest events + API reads + uptime checks.

The runner script (run_ingest_bench.sh) handles Docker orchestration, tc netem
latency injection, and cgroup memory sampling. This script focuses purely on
generating load and reporting request-level statistics.

Standalone usage (inside the bench container):
    python benchmarks/bench_ingest_memory.py --help
"""

import argparse
import json
import random
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import httpx

# Defaults
DEFAULT_HOST = "http://web:8000"
DEFAULT_CONCURRENCY = 500
DEFAULT_EVENTS_PER_WAVE = 2000
DEFAULT_WAVES = 5
DEFAULT_PAUSE = 3.0

API_TOKEN = "d" * 64


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_dsn_key(host: str, project_id: int) -> str:
    """Fetch the DSN public key via the API using the bootstrap dev token."""
    r = httpx.get(
        f"{host}/api/0/projects/",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        timeout=10,
    )
    r.raise_for_status()
    for proj in r.json():
        if str(proj.get("id")) == str(project_id):
            org_slug = proj["organization"]["slug"]
            proj_slug = proj["slug"]
            kr = httpx.get(
                f"{host}/api/0/projects/{org_slug}/{proj_slug}/keys/",
                headers={"Authorization": f"Bearer {API_TOKEN}"},
                timeout=10,
            )
            kr.raise_for_status()
            keys = kr.json()
            if keys:
                dsn = keys[0]["dsn"]["public"]
                return dsn.split("//")[1].split("@")[0]
    raise RuntimeError(f"Could not discover DSN key for project {project_id}")


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def make_envelope(sentry_key: str) -> bytes:
    """Build a realistic envelope payload (~2 KB) with a unique event_id."""
    event_id = "".join(random.choices("0123456789abcdef", k=32))
    ts = datetime.now(timezone.utc).isoformat()

    header = json.dumps(
        {
            "event_id": event_id,
            "sent_at": ts,
            "trace": {
                "trace_id": "".join(random.choices("0123456789abcdef", k=32)),
                "environment": "benchmark",
                "public_key": sentry_key,
            },
        }
    )
    item_header = json.dumps({"type": "event", "content_type": "application/json"})
    payload = json.dumps(
        {
            "message": f"Benchmark event {event_id[:8]}",
            "level": "info",
            "event_id": event_id,
            "timestamp": ts,
            "breadcrumbs": {
                "values": [
                    {
                        "category": "http",
                        "message": f"GET /api/v2/endpoint-{i}/ [200]",
                        "timestamp": ts,
                        "data": {"method": "GET", "status": 200},
                    }
                    for i in range(5)
                ]
            },
            "transaction": "/api/benchmark/test/",
            "contexts": {
                "runtime": {"name": "CPython", "version": "3.14.0"},
                "os": {"name": "Linux", "version": "6.1"},
            },
            "extra": {
                "payload_padding": "".join(random.choices(string.ascii_letters, k=512))
            },
            "environment": "benchmark",
            "server_name": f"bench-host-{random.randint(1, 16):02d}",
            "sdk": {"name": "sentry.python", "version": "2.0.0"},
            "platform": "python",
            "modules": {"django": "5.1", "celery": "5.4"},
            "request": {
                "url": "http://bench.example.com/api/test/",
                "method": "POST",
                "headers": {
                    "Content-Type": "application/json",
                    "User-Agent": "BenchmarkClient/1.0",
                },
            },
        }
    )
    return f"{header}\n{item_header}\n{payload}\n".encode()


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


class Stats:
    """Thread-safe request statistics collector."""

    def __init__(self):
        self._lock = threading.Lock()
        self.success = 0
        self.error = 0
        self.latencies: list[float] = []

    def record(self, elapsed: float, ok: bool):
        with self._lock:
            self.latencies.append(elapsed)
            if ok:
                self.success += 1
            else:
                self.error += 1

    def summary(self) -> dict:
        lats = sorted(self.latencies)
        n = len(lats)
        return {
            "total": n,
            "success": self.success,
            "error": self.error,
            "p50_ms": lats[n // 2] * 1000 if n else 0,
            "p95_ms": lats[int(n * 0.95)] * 1000 if n else 0,
            "p99_ms": lats[int(n * 0.99)] * 1000 if n else 0,
        }


def timed_request(
    client: httpx.Client, method: str, url: str, **kwargs
) -> tuple[float, bool]:
    """Execute a request and return (elapsed_secs, success_bool)."""
    t0 = time.monotonic()
    try:
        r = client.request(method, url, **kwargs)
        elapsed = time.monotonic() - t0
        return elapsed, r.status_code < 400
    except Exception:
        return time.monotonic() - t0, False


# ---------------------------------------------------------------------------
# Workload: ingest-only
# ---------------------------------------------------------------------------


def run_ingest_wave_into(
    client: httpx.Client,
    sentry_key: str,
    host: str,
    project_id: int,
    count: int,
    concurrency: int,
    stats: "Stats",
) -> None:
    """Send `count` envelope events at `concurrency` parallelism.

    Records results into the externally-supplied ``stats`` so the caller
    can keep per-wave and cumulative collectors separate.
    """
    endpoint = f"{host}/api/{project_id}/envelope/?sentry_key={sentry_key}"
    payloads = [make_envelope(sentry_key) for _ in range(count)]

    def send(payload: bytes):
        elapsed, ok = timed_request(
            client,
            "POST",
            endpoint,
            content=payload,
            headers={"Content-Type": "application/x-sentry-envelope"},
        )
        stats.record(elapsed, ok)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(send, p) for p in payloads]
        for f in as_completed(futures):
            f.result()


def run_ingest_wave(
    client: httpx.Client,
    sentry_key: str,
    host: str,
    project_id: int,
    count: int,
    concurrency: int,
) -> dict:
    """Backwards-compatible wrapper that returns a summary dict."""
    stats = Stats()
    run_ingest_wave_into(
        client, sentry_key, host, project_id, count, concurrency, stats
    )
    return stats.summary()


# ---------------------------------------------------------------------------
# Workload: probe (synthetic async-DB endpoint)
# ---------------------------------------------------------------------------


def run_probe_wave_into(
    client: httpx.Client,
    host: str,
    count: int,
    concurrency: int,
    stats: "Stats",
    probe_path: str = "/api/_probe/async/",
) -> None:
    """Hammer one of the async-DB probe endpoints (see
    :mod:`glitchtip.async_probe`).

    ``/api/_probe/async/`` — three trivial SELECTs back to back. Pure
    async-cursor synthetic.

    ``/api/_probe/realistic/`` — fast SELECT, ~0.5 ms Python CPU,
    async-backend ORM ``aget``, ~0.5 ms Python CPU, ``pg_sleep(0.01)``.
    Better target for "does async + Rust actually beat psycopg3 in a
    realistic shape?".

    Both bypass the ingest pipeline; every request goes through the
    IngestDispatcher minimal-middleware chain.
    """
    endpoint = f"{host}{probe_path}"

    def send(_):
        elapsed, ok = timed_request(client, "GET", endpoint)
        stats.record(elapsed, ok)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(send, i) for i in range(count)]
        for f in as_completed(futures):
            f.result()


# ---------------------------------------------------------------------------
# Workload: mixed (realistic)
# ---------------------------------------------------------------------------

# Weights control the relative frequency of each request type.
# These approximate a realistic production mix:
#   - Ingest dominates (SDKs sending events continuously)
#   - Uptime checks are frequent (heartbeat monitors)
#   - API reads are moderate (dashboard users browsing)
MIXED_WEIGHTS = {
    "ingest": 60,  # Event envelope POST
    "uptime": 20,  # Uptime heartbeat check
    "list_issues": 8,  # GET /api/0/organizations/{slug}/issues/
    "list_projects": 4,  # GET /api/0/projects/
    "get_org": 4,  # GET /api/0/organizations/{slug}/
    "get_project": 4,  # GET /api/0/projects/{org}/{proj}/
}


def build_mixed_request_list(count: int) -> list[str]:
    """Build a shuffled list of `count` request type names based on weights."""
    types = []
    total_weight = sum(MIXED_WEIGHTS.values())
    for req_type, weight in MIXED_WEIGHTS.items():
        n = max(1, round(count * weight / total_weight))
        types.extend([req_type] * n)
    # Trim or pad to exact count
    random.shuffle(types)
    return types[:count]


def run_mixed_wave_into(
    client: httpx.Client,
    sentry_key: str,
    host: str,
    project_id: int,
    count: int,
    concurrency: int,
    per_type_stats: dict[str, "Stats"],
) -> None:
    """Run a mixed workload wave, recording into the supplied stats dict."""
    endpoint_ingest = f"{host}/api/{project_id}/envelope/?sentry_key={sentry_key}"
    auth_headers = {"Authorization": f"Bearer {API_TOKEN}"}

    request_types = build_mixed_request_list(count)
    for rt in set(request_types):
        per_type_stats.setdefault(rt, Stats())

    # Pre-build ingest payloads
    ingest_payloads = iter(
        [make_envelope(sentry_key) for _ in range(request_types.count("ingest"))]
    )
    ingest_lock = threading.Lock()

    def execute(req_type: str):
        if req_type == "ingest":
            with ingest_lock:
                payload = next(ingest_payloads)
            elapsed, ok = timed_request(
                client,
                "POST",
                endpoint_ingest,
                content=payload,
                headers={"Content-Type": "application/x-sentry-envelope"},
            )
        elif req_type == "uptime":
            # Heartbeat check — hits cache/DB for monitor lookup
            # Use a fake endpoint_id; will 404 but still exercises the path
            elapsed, ok = timed_request(
                client,
                "GET",
                f"{host}/api/0/organizations/org/heartbeat_check/00000000-0000-0000-0000-000000000000/",
            )
            # 404/405 is expected for fake endpoint; count as "ok" for benchmarking
            ok = True
        elif req_type == "list_issues":
            elapsed, ok = timed_request(
                client,
                "GET",
                f"{host}/api/0/organizations/org/issues/?query=is:unresolved",
                headers=auth_headers,
            )
        elif req_type == "list_projects":
            elapsed, ok = timed_request(
                client,
                "GET",
                f"{host}/api/0/projects/",
                headers=auth_headers,
            )
        elif req_type == "get_org":
            elapsed, ok = timed_request(
                client,
                "GET",
                f"{host}/api/0/organizations/org/",
                headers=auth_headers,
            )
        elif req_type == "get_project":
            elapsed, ok = timed_request(
                client,
                "GET",
                f"{host}/api/0/projects/org/project/",
                headers=auth_headers,
            )
        else:
            return
        per_type_stats[req_type].record(elapsed, ok)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(execute, rt) for rt in request_types]
        for f in as_completed(futures):
            f.result()


def run_mixed_wave(
    client: httpx.Client,
    sentry_key: str,
    host: str,
    project_id: int,
    count: int,
    concurrency: int,
) -> dict[str, dict]:
    """Backwards-compatible wrapper returning a per-type summary dict."""
    per_type_stats: dict[str, Stats] = {
        rt: Stats() for rt in MIXED_WEIGHTS.keys()
    }
    run_mixed_wave_into(
        client, sentry_key, host, project_id, count, concurrency, per_type_stats
    )
    return {rt: s.summary() for rt, s in per_type_stats.items()}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def wait_for_server(host: str, timeout: int = 60):
    """Block until the web server responds to health checks."""
    print(f"Waiting for {host} to be ready...", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{host}/_health/", timeout=3)
            if r.status_code < 500:
                print("Server is ready.", flush=True)
                return
        except Exception:
            pass
        time.sleep(1)
    print("WARNING: Server may not be ready, proceeding anyway.", flush=True)


def print_stats_row(wave: int, stats: dict):
    """Print a single row of ingest stats."""
    print(
        f"  {wave:<6} {stats['success']:>8} {stats['error']:>8} "
        f"{stats['p50_ms']:>8.1f} {stats['p95_ms']:>8.1f} {stats['p99_ms']:>8.1f}"
    )


def print_mixed_stats(wave: int, per_type: dict[str, dict]):
    """Print stats for a mixed workload wave."""
    print(f"  Wave {wave}:")
    for rt in sorted(per_type):
        s = per_type[rt]
        print(
            f"    {rt:<16} ok={s['success']:>4}  err={s['error']:>3}"
            f"  p50={s['p50_ms']:>8.1f}ms  p95={s['p95_ms']:>8.1f}ms"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Memory growth benchmark with artificial DB latency",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Ingest-only, 500 concurrent, 5 waves of 2000 events
  python benchmarks/bench_ingest_memory.py

  # Mixed workload, 300 concurrent, 8 waves of 1500 requests
  python benchmarks/bench_ingest_memory.py --mode mixed -c 300 -n 1500 -w 8

  # Quick smoke test
  python benchmarks/bench_ingest_memory.py -c 50 -n 200 -w 2 --pause 1
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["ingest", "mixed", "probe", "probe-realistic"],
        default="ingest",
        help=(
            "Workload mode: ingest-only, mixed (events+API+uptime), probe "
            "(synthetic /api/_probe/async/ — 3 raw async SELECTs), or "
            "probe-realistic (/api/_probe/realistic/ — raw SQL + Python "
            "CPU + async ORM + Python CPU + pg_sleep)"
        ),
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Concurrent requests (default: {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "-n",
        "--requests-per-wave",
        type=int,
        default=DEFAULT_EVENTS_PER_WAVE,
        help=f"Requests per wave (default: {DEFAULT_EVENTS_PER_WAVE})",
    )
    parser.add_argument(
        "-w",
        "--waves",
        type=int,
        default=DEFAULT_WAVES,
        help=f"Number of waves (default: {DEFAULT_WAVES})",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=DEFAULT_PAUSE,
        help=f"Seconds between waves (default: {DEFAULT_PAUSE})",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help=f"Target host URL (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--project-id",
        type=int,
        default=1,
        help="Project ID for ingest (default: 1)",
    )
    parser.add_argument(
        "--json-out",
        type=str,
        default=None,
        help="Write aggregated results as JSON to this path (for compare scripts)",
    )
    args = parser.parse_args()

    host = args.host
    wait_for_server(host)

    if args.mode in ("probe", "probe-realistic"):
        sentry_key = ""  # not used by the probe endpoints
    else:
        print("Discovering DSN key...", flush=True)
        sentry_key = discover_dsn_key(host, args.project_id)
        print(f"  Key: {sentry_key[:8]}...{sentry_key[-4:]}", flush=True)

    mode_label = {
        "ingest": "INGEST-ONLY",
        "mixed": "MIXED WORKLOAD",
        "probe": "ASYNC-DB PROBE",
        "probe-realistic": "ASYNC-DB PROBE (realistic)",
    }[args.mode]
    print()
    print("=" * 70)
    print(f"MEMORY BENCHMARK — {mode_label}")
    print(f"  Target:        {host}")
    print(f"  Concurrency:   {args.concurrency}")
    print(f"  Reqs/wave:     {args.requests_per_wave}")
    print(f"  Waves:         {args.waves}")
    print(f"  Pause:         {args.pause}s between waves")
    if args.mode == "mixed":
        print(f"  Mix weights:   {MIXED_WEIGHTS}")
    print("=" * 70)
    print()

    # Aggregated stats across every wave — the JSON output consumer
    # (run_concurrency_bench.sh) uses these to compare backends.
    overall = Stats()
    wall_t0 = time.monotonic()

    transport = httpx.HTTPTransport(retries=0)
    with httpx.Client(timeout=60, transport=transport) as client:
        if args.mode == "ingest":
            header = f"{'Wave':<8} {'Success':>8} {'Error':>8} {'p50ms':>8} {'p95ms':>8} {'p99ms':>8}"
            print(header)
            print("-" * len(header))

            for wave in range(1, args.waves + 1):
                wave_stats = Stats()
                run_ingest_wave_into(
                    client,
                    sentry_key,
                    host,
                    args.project_id,
                    args.requests_per_wave,
                    args.concurrency,
                    wave_stats,
                )
                with overall._lock:
                    overall.success += wave_stats.success
                    overall.error += wave_stats.error
                    overall.latencies.extend(wave_stats.latencies)
                print_stats_row(wave, wave_stats.summary())
                if wave < args.waves:
                    print(f"  ... pausing {args.pause}s ...", flush=True)
                    time.sleep(args.pause)

        elif args.mode in ("probe", "probe-realistic"):
            probe_path = (
                "/api/_probe/realistic/"
                if args.mode == "probe-realistic"
                else "/api/_probe/async/"
            )
            header = (
                f"{'Wave':<8} {'Success':>8} {'Error':>8} "
                f"{'p50ms':>8} {'p95ms':>8} {'p99ms':>8}"
            )
            print(header)
            print("-" * len(header))

            for wave in range(1, args.waves + 1):
                wave_stats = Stats()
                run_probe_wave_into(
                    client,
                    host,
                    args.requests_per_wave,
                    args.concurrency,
                    wave_stats,
                    probe_path=probe_path,
                )
                with overall._lock:
                    overall.success += wave_stats.success
                    overall.error += wave_stats.error
                    overall.latencies.extend(wave_stats.latencies)
                print_stats_row(wave, wave_stats.summary())
                if wave < args.waves:
                    print(f"  ... pausing {args.pause}s ...", flush=True)
                    time.sleep(args.pause)

        else:  # mixed
            for wave in range(1, args.waves + 1):
                per_type = {
                    rt: Stats() for rt in MIXED_WEIGHTS.keys()
                }
                run_mixed_wave_into(
                    client,
                    sentry_key,
                    host,
                    args.project_id,
                    args.requests_per_wave,
                    args.concurrency,
                    per_type,
                )
                with overall._lock:
                    for s in per_type.values():
                        overall.success += s.success
                        overall.error += s.error
                        overall.latencies.extend(s.latencies)
                print_mixed_stats(wave, {rt: s.summary() for rt, s in per_type.items()})
                if wave < args.waves:
                    print(f"  ... pausing {args.pause}s ...", flush=True)
                    time.sleep(args.pause)

    wall_elapsed = time.monotonic() - wall_t0

    print()
    print("=" * 70)
    print("BENCHMARK COMPLETE")
    print("=" * 70)

    if args.json_out:
        import json as _json

        s = overall.summary()
        total = s["success"] + s["error"]
        lats_sorted = sorted(overall.latencies)
        n = len(lats_sorted)
        out = {
            "mode": args.mode,
            "concurrency": args.concurrency,
            "requests_per_wave": args.requests_per_wave,
            "waves": args.waves,
            "wall_elapsed_s": wall_elapsed,
            "overall": {
                "total": total,
                "success": s["success"],
                "error": s["error"],
                "error_rate": (s["error"] / total) if total else 0.0,
                "req_per_s": (total / wall_elapsed) if wall_elapsed > 0 else 0.0,
                "p50_ms": s["p50_ms"],
                "p95_ms": s["p95_ms"],
                "p99_ms": s["p99_ms"],
                "p999_ms": (
                    lats_sorted[int(n * 0.999)] * 1000 if n else 0.0
                ),
            },
        }
        with open(args.json_out, "w") as f:
            _json.dump(out, f, indent=2)
        print(f"  wrote {args.json_out}")


if __name__ == "__main__":
    main()
