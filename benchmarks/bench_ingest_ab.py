#!/usr/bin/env python
"""A/B benchmark: Python vs Rust envelope ingest (GLITCHTIP_RUST_INGEST).

Drives two production-shaped GlitchTip servers (same image, flag off/on —
see compose.ingest_ab.yml) with identical workloads in interleaved
A/B/A/B segments, and measures what the rust-ingest plan cares about:

  * CPU seconds per 10k accepted envelopes (process_cpu_seconds_total of
    the granian worker, scraped from /metrics before and after each
    segment — the segment only ends once the embedded worker has drained,
    so enqueue AND processing cost are attributed to the segment).
  * RSS growth per 10k accepted envelopes (least-squares slope of settled
    process_resident_memory_bytes across prodmix segments).
  * Post-burst settled RSS (idle -> burst -> idle workload).
  * Throughput (RPS), last.

Workloads:
  prodmix     55% error events / 20% transactions / 10% log envelopes /
              15% ignored items (session, client_report). Payload sizes are
              log-normal (median 25 KB) with a tail clamped at 2 MiB;
              payloads over 4 KiB are gzipped like real SDKs.
  junk        fast-reject flood: unknown item types, malformed envelope
              headers, wrong DSN keys.
  oversized   6 MiB bodies over the 5 MiB unzipped cap -> 413.
  header_dsn  DSN only in the envelope header (no query/auth header).
              KNOWN divergence: Rust accepts (200), Python rejects (403).
  burst       idle -> high-concurrency burst -> idle, for settled RSS.

The runner script (run_ingest_ab.sh) handles Docker orchestration; this
script runs inside the bench container:

    python benchmarks/bench_ingest_ab.py --help
"""

import argparse
import gzip
import json
import math
import random
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import httpx

API_TOKEN = "d" * 64  # bootstrap_dev token, present in both arm databases
DEFAULT_ARMS = "py=http://web-py:8000,rust=http://web-rust:8000"

GZIP_THRESHOLD = 4096  # sentry SDKs compress payloads beyond ~this size
SIZE_CLAMP = (2 * 1024, 2 * 1024 * 1024)
OVERSIZED_BODY = 6 * 1024 * 1024  # over the 5 MiB unzipped cap

PRODMIX_WEIGHTS = {"event": 55, "transaction": 20, "log": 10, "ignored": 15}

# Fixed identity pools so events group into a bounded set of issues /
# transaction groups instead of creating one DB row-tree per request.
ERROR_KINDS = [
    ("TypeError", "unsupported operand type(s) for +: 'int' and 'str'"),
    ("ValueError", "invalid literal for int() with base 10: 'abc'"),
    ("KeyError", "'user_id'"),
    ("ConnectionError", "Connection refused by upstream"),
    ("TimeoutError", "Request timed out after 30s"),
    ("AttributeError", "'NoneType' object has no attribute 'save'"),
    ("RuntimeError", "Event loop is closed"),
    ("PermissionError", "Access denied to resource"),
    ("ZeroDivisionError", "division by zero"),
    ("LookupError", "Row not found in partition"),
]
TRANSACTION_NAMES = [f"GET /api/resource-{i}/" for i in range(10)]


# ---------------------------------------------------------------------------
# Random text pool: slices are unique enough that gzip can't collapse them,
# but word-salad text keeps a realistic (~2-3x) compression ratio. Building
# per-payload random strings with random.choices would dominate generator CPU.
# ---------------------------------------------------------------------------

_TEXT_POOL = ""


def init_text_pool() -> None:
    """Built after random.seed(args.seed) so --seed reproduces payload
    content run-to-run, not just the request mix."""
    global _TEXT_POOL
    words = [
        "".join(random.choices(string.ascii_lowercase, k=random.randint(3, 10)))
        for _ in range(512)
    ]
    _TEXT_POOL = " ".join(random.choices(words, k=1_200_000))  # ~8 MiB


def rand_text(n: int) -> str:
    start = random.randint(0, len(_TEXT_POOL) - n - 1)
    return _TEXT_POOL[start : start + n]


def lognormal_size(median_bytes: float, sigma: float) -> int:
    size = random.lognormvariate(math.log(median_bytes), sigma)
    return int(min(max(size, SIZE_CLAMP[0]), SIZE_CLAMP[1]))


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _event_id() -> str:
    return "".join(random.choices("0123456789abcdef", k=32))


def _finish(header: dict, item_header: dict, payload: bytes) -> tuple[bytes, dict]:
    """Assemble an envelope and gzip it like an SDK would."""
    body = (
        json.dumps(header).encode()
        + b"\n"
        + json.dumps(item_header).encode()
        + b"\n"
        + payload
        + b"\n"
    )
    headers = {"Content-Type": "application/x-sentry-envelope"}
    if len(body) > GZIP_THRESHOLD:
        body = gzip.compress(body, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
    return body, headers


def make_error_event(size: int, header_extra: dict | None = None) -> tuple[bytes, dict]:
    event_id = _event_id()
    ts = datetime.now(timezone.utc).isoformat()
    exc_type, exc_value = random.choice(ERROR_KINDS)
    payload = json.dumps(
        {
            "event_id": event_id,
            "timestamp": ts,
            "platform": "python",
            "level": "error",
            "environment": "benchmark",
            "release": "bench@1.0.0",
            "server_name": f"bench-host-{random.randint(1, 16):02d}",
            "sdk": {"name": "sentry.python", "version": "2.0.0"},
            "exception": {
                "values": [
                    {
                        "type": exc_type,
                        "value": exc_value,
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": f"app/module_{i}.py",
                                    "function": f"handler_{i}",
                                    "in_app": True,
                                    "lineno": 10 + i,
                                }
                                for i in range(8)
                            ]
                        },
                    }
                ]
            },
            "request": {
                "url": "http://bench.example.com/api/test/",
                "method": "POST",
                "headers": {"User-Agent": "BenchmarkClient/1.0"},
            },
            "extra": {"payload_padding": rand_text(size)},
        }
    ).encode()
    header = {"event_id": event_id, "sent_at": ts, **(header_extra or {})}
    return _finish(
        header, {"type": "event", "content_type": "application/json"}, payload
    )


def make_transaction(size: int) -> tuple[bytes, dict]:
    event_id = _event_id()
    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    payload = json.dumps(
        {
            "event_id": event_id,
            "type": "transaction",
            "transaction": random.choice(TRANSACTION_NAMES),
            "contexts": {
                "trace": {
                    "trace_id": _event_id(),
                    "span_id": "aaaabbbbccccdddd",
                    "op": "http.server",
                }
            },
            "start_timestamp": ts,
            "timestamp": ts,
            "spans": [],
            "extra": {"payload_padding": rand_text(size)},
        }
    ).encode()
    return _finish(
        {"event_id": event_id, "sent_at": ts}, {"type": "transaction"}, payload
    )


def make_log_envelope(size: int) -> tuple[bytes, dict]:
    n_records = min(max(size // 400, 1), 100)
    payload = json.dumps(
        {
            "items": [
                {
                    "timestamp": time.time(),
                    "level": random.choice(["info", "warn", "error"]),
                    "body": f"bench log record: {rand_text(300)}",
                    "attributes": {
                        "sentry.service": {"value": "bench", "type": "string"}
                    },
                }
                for _ in range(n_records)
            ]
        }
    ).encode()
    return _finish(
        {"sent_at": datetime.now(timezone.utc).isoformat()}, {"type": "log"}, payload
    )


def make_ignored_item() -> tuple[bytes, dict]:
    """Item types every SDK sends that GlitchTip drops without parsing."""
    ts = datetime.now(timezone.utc).isoformat()
    if random.random() < 0.5:
        item_type, payload = (
            "session",
            json.dumps(
                {
                    "sid": _event_id(),
                    "init": True,
                    "started": ts,
                    "timestamp": ts,
                    "status": "ok",
                    "attrs": {"release": "bench@1.0.0"},
                }
            ).encode(),
        )
    else:
        item_type, payload = (
            "client_report",
            json.dumps(
                {
                    "timestamp": ts,
                    "discarded_events": [
                        {"reason": "queue_overflow", "category": "error", "quantity": 2}
                    ],
                }
            ).encode(),
        )
    return _finish({"sent_at": ts}, {"type": item_type}, payload)


def make_junk() -> tuple[bytes, dict, bool]:
    """Fast-reject shapes. Returns (body, headers, needs_bad_key)."""
    roll = random.random()
    if roll < 0.4:  # unknown item type: 200, item dropped
        body, headers = _finish(
            {"sent_at": datetime.now(timezone.utc).isoformat()},
            {"type": "quantum_report"},
            b'{"data": "junk"}',
        )
        return body, headers, False
    if roll < 0.7:  # malformed envelope header
        return (
            b'{"event_id": not json\ngarbage\n',
            {"Content-Type": "application/x-sentry-envelope"},
            False,
        )
    # wrong DSN key: 403 before the body is ever parsed
    body, headers = make_error_event(2048)
    return body, headers, True


_OVERSIZED_PADDING = None


def make_oversized() -> tuple[bytes, dict]:
    """A body over the unzipped cap. Padding is built once — the servers
    reject on size, so identical content across requests changes nothing."""
    global _OVERSIZED_PADDING
    if _OVERSIZED_PADDING is None:
        _OVERSIZED_PADDING = rand_text(OVERSIZED_BODY)
    event_id = _event_id()
    ts = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(
        {
            "event_id": event_id,
            "timestamp": ts,
            "platform": "python",
            "message": "oversized",
            "extra": {"payload_padding": _OVERSIZED_PADDING},
        }
    ).encode()
    body = (
        json.dumps({"event_id": event_id, "sent_at": ts}).encode()
        + b"\n"
        + json.dumps({"type": "event"}).encode()
        + b"\n"
        + payload
        + b"\n"
    )
    # Deliberately NOT gzipped: the uncompressed-body 413 path.
    return body, {"Content-Type": "application/x-sentry-envelope"}


# ---------------------------------------------------------------------------
# Arm: one server under test
# ---------------------------------------------------------------------------


class Arm:
    def __init__(self, name: str, base_url: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.project_id: int | None = None
        self.sentry_key: str | None = None
        self.dsn: str | None = None

    def wait_ready(self, timeout: int = 600) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{self.base_url}/_health/", timeout=3)
                if r.status_code < 500:
                    return
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError(f"{self.name}: server not ready in {timeout}s")

    def discover_dsn(self) -> None:
        r = httpx.get(
            f"{self.base_url}/api/0/projects/",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=10,
        )
        r.raise_for_status()
        projects = r.json()
        if not projects:
            raise RuntimeError(f"{self.name}: no projects — bootstrap_dev missing?")
        proj = projects[0]
        self.project_id = int(proj["id"])
        kr = httpx.get(
            f"{self.base_url}/api/0/projects/{proj['organization']['slug']}/{proj['slug']}/keys/",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=10,
        )
        kr.raise_for_status()
        self.dsn = kr.json()[0]["dsn"]["public"]
        self.sentry_key = self.dsn.split("//")[1].split("@")[0]

    @property
    def envelope_url(self) -> str:
        return f"{self.base_url}/api/{self.project_id}/envelope/?sentry_key={self.sentry_key}"

    @property
    def envelope_url_bare(self) -> str:
        return f"{self.base_url}/api/{self.project_id}/envelope/"

    def scrape(self) -> dict:
        """CPU / RSS / start-time of the (single) granian worker process."""
        r = httpx.get(f"{self.base_url}/metrics", timeout=10)
        r.raise_for_status()
        out = {}
        for line in r.text.splitlines():
            for field, series in (
                ("cpu_s", "process_cpu_seconds_total"),
                ("rss", "process_resident_memory_bytes"),
                ("start_time", "process_start_time_seconds"),
            ):
                if line.startswith(series + " "):
                    out[field] = float(line.split()[1])
        missing = {"cpu_s", "rss", "start_time"} - out.keys()
        if missing:
            raise RuntimeError(f"{self.name}: /metrics missing {missing}")
        return out

    def wait_drained(
        self, poll_s: float = 1.0, idle_cpu_s: float = 0.05, timeout: float = 600.0
    ) -> tuple[float, bool]:
        """Block until the embedded worker has gone idle: process CPU delta
        below `idle_cpu_s` for 3 consecutive polls. Returns (waited, drained)."""
        t0 = time.monotonic()
        prev = self.scrape()["cpu_s"]
        quiet = 0
        while time.monotonic() - t0 < timeout:
            time.sleep(poll_s)
            cur = self.scrape()["cpu_s"]
            if cur - prev < idle_cpu_s:
                quiet += 1
                if quiet >= 3:
                    return time.monotonic() - t0, True
            else:
                quiet = 0
            prev = cur
        return time.monotonic() - t0, False


# ---------------------------------------------------------------------------
# Load engine
# ---------------------------------------------------------------------------


class SegmentStats:
    def __init__(self):
        self._lock = threading.Lock()
        self.statuses: dict[int, int] = {}
        self.errors = 0
        self.accepted_events = 0
        self.latencies: list[float] = []

    def record(self, status: int | None, elapsed: float, is_event: bool):
        with self._lock:
            self.latencies.append(elapsed)
            if status is None:
                self.errors += 1
            else:
                self.statuses[status] = self.statuses.get(status, 0) + 1
                if is_event and 200 <= status < 300:
                    self.accepted_events += 1

    @property
    def accepted(self) -> int:
        return sum(n for code, n in self.statuses.items() if 200 <= code < 300)


def fire(
    client: httpx.Client,
    requests: list[tuple[str, bytes, dict, bool]],
    concurrency: int,
) -> SegmentStats:
    stats = SegmentStats()

    def send(req):
        url, body, headers, is_event = req
        t0 = time.monotonic()
        try:
            r = client.post(url, content=body, headers=headers)
            stats.record(r.status_code, time.monotonic() - t0, is_event)
        except Exception:
            stats.record(None, time.monotonic() - t0, is_event)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(send, req) for req in requests]
        for f in as_completed(futures):
            f.result()
    return stats


def build_requests(
    arm: Arm, workload: str, n: int, args
) -> list[tuple[str, bytes, dict, bool]]:
    """Request tuples are (url, body, headers, is_event) — is_event marks
    requests that carry an ingestible event/transaction/log, so accepted
    ENVELOPES (2xx) and accepted EVENTS can be counted separately (ignored
    item types also 200)."""
    reqs = []
    if workload == "prodmix":
        kinds = []
        total = sum(PRODMIX_WEIGHTS.values())
        for kind, weight in PRODMIX_WEIGHTS.items():
            kinds.extend([kind] * max(1, round(n * weight / total)))
        random.shuffle(kinds)
        for kind in kinds[:n]:
            size = lognormal_size(args.median_kb * 1024, args.sigma)
            if kind == "event":
                body, headers = make_error_event(size)
            elif kind == "transaction":
                body, headers = make_transaction(size)
            elif kind == "log":
                body, headers = make_log_envelope(size)
            else:
                body, headers = make_ignored_item()
            reqs.append((arm.envelope_url, body, headers, kind != "ignored"))
    elif workload == "junk":
        for _ in range(n):
            body, headers, bad_key = make_junk()
            url = (
                f"{arm.envelope_url_bare}?sentry_key={'0' * 32}"
                if bad_key
                else arm.envelope_url
            )
            reqs.append((url, body, headers, False))
    elif workload == "oversized":
        body, headers = make_oversized()
        reqs = [(arm.envelope_url, body, headers, False)] * n
    elif workload == "header_dsn":
        for _ in range(n):
            body, headers = make_error_event(4096, header_extra={"dsn": arm.dsn})
            reqs.append((arm.envelope_url_bare, body, headers, True))
    else:
        raise ValueError(workload)
    return reqs


# ---------------------------------------------------------------------------
# Segment runner
# ---------------------------------------------------------------------------


_WORKLOAD_IDS = {
    "prodmix": 1,
    "junk": 2,
    "oversized": 3,
    "header_dsn": 4,
    "burst": 5,
    "warmup": 6,
}


def reseed(args, workload: str, seg: int) -> None:
    """Reseed per (workload, segment) so both arms build byte-identical
    request lists — otherwise they consume different slices of the global
    RNG stream and the workloads are only statistically similar.

    ``run_nonce`` (fresh entropy per run) is mixed in so a rerun against a
    warm stack never regenerates the previous run's event ids: the server
    dedupes event ids in cache for ~5 minutes, and replayed ids would keep
    the 200 responses while silently skipping enqueue + worker processing.
    """
    random.seed(
        args.run_nonce * 2_147_483_659
        + args.seed * 1_000_003
        + _WORKLOAD_IDS[workload] * 1_009
        + seg
    )


def run_segment(client: httpx.Client, arm: Arm, workload: str, seg: int, args) -> dict:
    reseed(args, workload, seg)
    reqs = build_requests(arm, workload, args.requests_for(workload), args)
    before = arm.scrape()
    t0 = time.monotonic()
    stats = fire(client, reqs, args.concurrency)
    wall = time.monotonic() - t0
    drain_s, drained = arm.wait_drained(timeout=args.drain_timeout)
    time.sleep(args.settle)
    after = arm.scrape()

    lats = sorted(stats.latencies)
    record = {
        "workload": workload,
        "seg": seg,
        "arm": arm.name,
        "n": len(reqs),
        "accepted": stats.accepted,
        "statuses": {str(k): v for k, v in sorted(stats.statuses.items())},
        "errors": stats.errors,
        "wall_s": round(wall, 2),
        "rps": round(len(reqs) / wall, 1) if wall else 0.0,
        "p50_ms": round(lats[len(lats) // 2] * 1000, 1) if lats else 0.0,
        "p95_ms": round(lats[int(len(lats) * 0.95)] * 1000, 1) if lats else 0.0,
        "accepted_events": stats.accepted_events,
        "cpu_s": round(after["cpu_s"] - before["cpu_s"], 3),
        "drain_s": round(drain_s, 1),
        "drained": drained,
        "rss_settled_mb": round(after["rss"] / 1048576, 1),
        "restarted": after["start_time"] != before["start_time"],
    }
    flags = ""
    if record["restarted"]:
        flags += "  !! WORKER RESTARTED — segment invalid"
    if not drained:
        flags += "  !! drain timeout — segment invalid"
    print(
        f"  [{workload}] seg {seg} {arm.name:>4}: "
        f"acc={record['accepted']:>5}/{record['n']} err={record['errors']} "
        f"rps={record['rps']:>7} cpu={record['cpu_s']:>7.2f}s "
        f"drain={record['drain_s']:>5.1f}s rss={record['rss_settled_mb']:>6.1f}MB"
        f"{flags}",
        flush=True,
    )
    return record


def run_burst(client: httpx.Client, arm: Arm, args) -> dict:
    """idle -> burst -> idle: does RSS settle back down?"""
    time.sleep(args.settle)
    baseline = arm.scrape()

    peak_rss = [baseline["rss"]]
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            try:
                peak_rss.append(arm.scrape()["rss"])
            except Exception:
                pass
            stop.wait(1.0)

    t = threading.Thread(target=sampler, daemon=True)
    t.start()
    reseed(args, "burst", 0)
    reqs = build_requests(arm, "prodmix", args.burst_n, args)
    t0 = time.monotonic()
    stats = fire(client, reqs, args.burst_concurrency)
    wall = time.monotonic() - t0
    _, drained = arm.wait_drained(timeout=args.drain_timeout)
    stop.set()
    t.join()

    time.sleep(args.burst_idle)
    settled = arm.scrape()
    record = {
        "workload": "burst",
        "arm": arm.name,
        "n": args.burst_n,
        "accepted": stats.accepted,
        "errors": stats.errors,
        "rps": round(args.burst_n / wall, 1) if wall else 0.0,
        "baseline_rss_mb": round(baseline["rss"] / 1048576, 1),
        "peak_rss_mb": round(max(peak_rss) / 1048576, 1),
        "settled_rss_mb": round(settled["rss"] / 1048576, 1),
        "drained": drained,
        "restarted": settled["start_time"] != baseline["start_time"],
    }
    flags = ""
    if record["restarted"]:
        flags += "  !! WORKER RESTARTED"
    if not drained:
        flags += "  !! drain timeout — settled RSS not settled"
    print(
        f"  [burst] {arm.name:>4}: acc={record['accepted']}/{record['n']} "
        f"rss base={record['baseline_rss_mb']}MB peak={record['peak_rss_mb']}MB "
        f"settled={record['settled_rss_mb']}MB{flags}",
        flush=True,
    )
    return record


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def lstsq(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least-squares slope and R**2."""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return 0.0, 0.0
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy) if syy else 1.0
    return slope, r2


def median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] + xs[(n - 1) // 2]) / 2 if xs else 0.0


def aggregate(segments: list[dict], arm: str, workload: str) -> dict | None:
    segs = [
        s
        for s in segments
        if s["arm"] == arm
        and s["workload"] == workload
        and not s["restarted"]
        and s["drained"]  # a timed-out drain undercounts CPU and bleeds
        # leftover processing into the same arm's next segment
    ]
    if not segs:
        return None
    total_n = sum(s["n"] for s in segs)
    total_accepted = sum(s["accepted"] for s in segs)
    total_events = sum(s["accepted_events"] for s in segs)
    total_cpu = sum(s["cpu_s"] for s in segs)
    total_wall = sum(s["wall_s"] for s in segs)
    # prodmix normalizes per accepted EVENT (ignored item types also return
    # 200, so plain 2xx would inflate the denominator ~18%); the mostly-
    # rejected-by-design workloads normalize per request.
    denom = total_events if workload == "prodmix" else total_n
    per_seg = [
        s["cpu_s"] / (s["accepted_events"] if workload == "prodmix" else s["n"]) * 1e4
        for s in segs
        if (s["accepted_events"] if workload == "prodmix" else s["n"])
    ]
    out = {
        "segments": len(segs),
        "requests": total_n,
        "accepted": total_accepted,
        "accepted_events": total_events,
        "errors": sum(s["errors"] for s in segs),
        "cpu_s": round(total_cpu, 2),
        "cpu_s_per_10k": round(total_cpu / denom * 10_000, 2) if denom else None,
        "cpu_s_per_10k_median": round(median(per_seg), 2) if per_seg else None,
        "rps": round(total_n / total_wall, 1) if total_wall else 0.0,
        "p95_ms_mean": round(sum(s["p95_ms"] for s in segs) / len(segs), 1),
    }
    if workload == "prodmix":
        cum, xs, ys = 0, [], []
        for s in segs:
            cum += s["accepted_events"]
            xs.append(float(cum))
            ys.append(s["rss_settled_mb"])
        # The first segment absorbs plateau/allocator warmup that the short
        # warmup pass doesn't burn off; a least-squares line over that
        # saturating curve would report the transient as slope.
        slope, r2 = lstsq(xs[1:], ys[1:])
        out["rss_slope_mb_per_10k"] = round(slope * 10_000, 2)
        out["rss_slope_r2"] = round(r2, 2)
        out["rss_first_mb"] = ys[0]
        out["rss_last_mb"] = ys[-1]
    return out


def print_comparison(results: dict, workloads: list[str], arms: list[str]) -> None:
    print()
    print("=" * 78)
    print("A/B SUMMARY")
    print("=" * 78)
    for workload in workloads:
        if workload == "burst":
            continue
        per_arm = {a: results["aggregates"].get(a, {}).get(workload) for a in arms}
        if not all(per_arm.values()):
            continue
        denom_label = "accepted events" if workload == "prodmix" else "requests"
        print(f"\n[{workload}]  (CPU normalized per 10k {denom_label})")
        keys = ["cpu_s_per_10k", "cpu_s_per_10k_median", "rps", "p95_ms_mean"]
        if workload == "prodmix":
            keys.insert(2, "rss_slope_mb_per_10k")
        header = (
            f"  {'metric':<24}" + "".join(f"{a:>12}" for a in arms) + f"{'delta':>12}"
        )
        print(header)
        for key in keys:
            vals = [per_arm[a].get(key) for a in arms]
            delta = ""
            if (
                len(vals) == 2
                and all(isinstance(v, (int, float)) for v in vals)
                and vals[0]
            ):
                delta = f"{(vals[1] - vals[0]) / abs(vals[0]) * 100:+.1f}%"
            print(f"  {key:<24}" + "".join(f"{v:>12}" for v in vals) + f"{delta:>12}")
    bursts = results.get("burst", [])
    if bursts:
        print("\n[burst]  idle -> burst -> idle")
        print(f"  {'metric':<24}" + "".join(f"{b['arm']:>12}" for b in bursts))
        for key in ("baseline_rss_mb", "peak_rss_mb", "settled_rss_mb"):
            print(f"  {key:<24}" + "".join(f"{b[key]:>12}" for b in bursts))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="A/B ingest benchmark: Python vs Rust envelope path"
    )
    parser.add_argument(
        "--arms",
        default=DEFAULT_ARMS,
        help=f"Comma-separated name=url pairs (default: {DEFAULT_ARMS})",
    )
    parser.add_argument(
        "--workloads",
        default="prodmix,junk,burst,oversized,header_dsn",
        help="Comma-separated subset of prodmix,junk,burst,oversized,"
        "header_dsn — run in the given order. The default runs burst before"
        " the two workloads whose RSS history is asymmetric BY DESIGN"
        " (oversized buffers on the Python arm only; header_dsn ingests on"
        " the Rust arm only), so the burst comparison starts from"
        " symmetric heaps.",
    )
    parser.add_argument(
        "--segments",
        type=int,
        default=4,
        help="Interleaved A/B segment pairs per workload (default 4)",
    )
    parser.add_argument(
        "-n",
        "--requests",
        type=int,
        default=2000,
        help="Requests per segment (default 2000; oversized uses n/20)",
    )
    parser.add_argument("-c", "--concurrency", type=int, default=100)
    parser.add_argument(
        "--median-kb",
        type=float,
        default=25.0,
        help="Log-normal payload median (default 25 KB)",
    )
    parser.add_argument(
        "--sigma",
        type=float,
        default=1.5,
        help="Log-normal sigma (default 1.5; tail clamped at 2 MiB)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=10.0,
        help="Idle seconds before the settled RSS sample",
    )
    parser.add_argument("--drain-timeout", type=float, default=600.0)
    parser.add_argument(
        "--warmup",
        type=int,
        default=1000,
        help="Unmeasured prodmix warmup requests per arm",
    )
    parser.add_argument("--burst-n", type=int, default=8000)
    parser.add_argument("--burst-concurrency", type=int, default=400)
    parser.add_argument(
        "--burst-idle",
        type=float,
        default=45.0,
        help="Idle seconds after the burst before the settled sample",
    )
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    args.requests_for = lambda workload: (
        max(50, args.requests // 20) if workload == "oversized" else args.requests
    )
    # Fresh entropy per run, mixed into every reseed() (see its docstring):
    # a rerun against a warm stack must not replay the previous run's event
    # ids into the server's ~5-minute dedupe window.
    args.run_nonce = random.SystemRandom().getrandbits(31)

    random.seed(args.seed)
    init_text_pool()
    arms = []
    for pair in args.arms.split(","):
        name, url = pair.split("=", 1)
        arms.append(Arm(name.strip(), url.strip()))
    workloads = [w.strip() for w in args.workloads.split(",") if w.strip()]

    print("Waiting for servers...", flush=True)
    for arm in arms:
        arm.wait_ready()
        arm.discover_dsn()
        print(
            f"  {arm.name}: {arm.base_url} project={arm.project_id} key={arm.sentry_key[:8]}...",
            flush=True,
        )

    print(
        f"\nA/B INGEST BENCHMARK — segments={args.segments} n={args.requests} "
        f"c={args.concurrency} median={args.median_kb}KB sigma={args.sigma} seed={args.seed}",
        flush=True,
    )

    # The pool must exceed the highest concurrency used or httpx silently
    # serializes the overflow and the burst never reaches the server.
    max_conc = max(args.concurrency, args.burst_concurrency)
    limits = httpx.Limits(
        max_connections=max_conc + 10, max_keepalive_connections=max_conc
    )
    transport = httpx.HTTPTransport(retries=0, limits=limits)
    segments: list[dict] = []
    bursts: list[dict] = []

    def dump_partial():
        # Persist raw data after every segment so a crash (scrape error,
        # container OOM) never discards an hour of measurements.
        if args.json_out:
            with open(args.json_out, "w") as f:
                json.dump(
                    {
                        "config": {
                            k: v for k, v in vars(args).items() if not callable(v)
                        },
                        "segments": segments,
                        "burst": bursts,
                    },
                    f,
                    indent=2,
                )

    with httpx.Client(timeout=120, transport=transport) as client:
        if args.warmup:
            print(f"\nWarmup ({args.warmup} prodmix requests per arm)...", flush=True)
            for arm in arms:
                reseed(args, "warmup", 0)
                fire(
                    client,
                    build_requests(arm, "prodmix", args.warmup, args),
                    args.concurrency,
                )
                arm.wait_drained(timeout=args.drain_timeout)

        for workload in workloads:
            print(f"\n=== workload: {workload} ===", flush=True)
            if workload == "burst":
                for arm in arms:
                    bursts.append(run_burst(client, arm, args))
                    dump_partial()
                continue
            for seg in range(args.segments):
                # Alternate which arm goes first so time-of-run drift
                # (DB growth, page cache) cancels out across segments.
                order = arms if seg % 2 == 0 else list(reversed(arms))
                for arm in order:
                    segments.append(run_segment(client, arm, workload, seg, args))
                    dump_partial()

    arm_names = [a.name for a in arms]
    aggregates = {}
    for a in arm_names:
        aggregates[a] = {}
        for w in workloads:
            if w == "burst":
                continue
            agg = aggregate(segments, a, w)
            if agg:
                aggregates[a][w] = agg
    results = {
        "config": {k: v for k, v in vars(args).items() if not callable(v)},
        "segments": segments,
        "burst": bursts,
        "aggregates": aggregates,
    }

    invalid = [s for s in segments if s["restarted"]]
    if invalid:
        print(
            f"\nWARNING: {len(invalid)} segment(s) saw a worker restart and were "
            "excluded from aggregates — raise GRANIAN_WORKERS_MAX_RSS / WEB_MEM.",
            flush=True,
        )

    print_comparison(results, workloads, arm_names)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
