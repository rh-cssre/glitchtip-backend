#!/usr/bin/env python
"""
Benchmark: time and peak RSS per event for each symbolication path.

Runs events through ``process_issue_events``, the same seam the symbolication
parity tests use, against the fixtures in
``apps/event_ingest/tests/test_data/symbolication``. So it keeps measuring
the same thing when the implementation behind that seam changes.

Each scenario runs in its own child process. After setup and a warm-up
event with nothing to symbolicate, it reports the first symbolicated event
(time, and how far RSS peaked above where it started) and then steady state
over ``--events`` more (time per event, peak RSS, and how far RSS peaked
above its level at the start of the run). Peaks are the process's high water
mark, reset through /proc/self/clear_refs. Each event is its own ingest
batch, as a steady trickle of events would arrive. A scratch
database (``test_symbolication_bench``) is created for the run and dropped
afterwards; uploads go to a temporary MEDIA_ROOT.

The ``plain`` scenario is an event with nothing to symbolicate, in a project
with no debug files: subtract it to see what symbolication adds.

Usage:
    docker compose run --rm web python benchmarks/bench_symbolication.py
    docker compose run --rm web python benchmarks/bench_symbolication.py \\
        --events 50 --scenario js_large --scenario native_no_debug_meta
    ... --json   # machine-readable output

Local only; not part of CI.
"""

import argparse
import contextlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

BENCH_DB = "test_symbolication_bench"
SCENARIOS = [
    "plain",
    "js_small",
    "js_large",
    "native_debug_meta",
    "native_no_debug_meta",
    "no_native_frames",
    "proguard",
]
# The many-debug-files scenarios use the shape the cost tests use: 50 debug
# ids, each uploaded 4 times.
DISTINCT_DEBUG_IDS = 50
UPLOADS_PER_DEBUG_ID = 4
PROGUARD_MAPPINGS = 20


def use_test_settings():
    """Load Django with the test settings: immediate task backend, LocMem
    cache, local file storage. TESTING is decided from argv."""
    sys.argv = [sys.argv[0], "test"]
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import django

    django.setup()


# --- Child: one scenario -----------------------------------------------------


def read_status_kib(field: str) -> int:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith(field + ":"):
                return int(line.split()[1])
    return 0


def reset_peak_rss():
    # Writing 5 resets VmHWM to the current RSS (Linux 4.0+).
    with open("/proc/self/clear_refs", "w") as f:
        f.write("5")


def run_child(scenario: str, events: int) -> dict:
    use_test_settings()
    from django.db import connections

    connections["default"].settings_dict["NAME"] = BENCH_DB

    from django.utils import timezone
    from model_bakery import baker

    from apps.event_ingest.process_event import process_issue_events
    from apps.event_ingest.schema import ErrorIssueEventSchema, IssueTaskMessage
    from apps.event_ingest.tests.symbolication import (
        ELF_DEBUG_ID,
        ELF_IMAGE_ADDR,
        JS_FIXTURES,
        PROGUARD_FIXTURES,
        crash_frame_offsets,
        debug_id_bundle,
        make_large_bundle,
        measure_symbolication,
        upload_artifact_bundle,
        upload_crash_sources,
        upload_native_project,
        upload_proguard_mapping,
    )
    from apps.event_ingest.tests.utils import run_async_closing

    project = baker.make("projects.Project", organization__scrub_ip_addresses=False)
    organization = project.organization

    def upload_elf_and_sources():
        upload_native_project(project, 1, 1)
        upload_crash_sources(project, ELF_DEBUG_ID)

    def upload_many_elfs():
        upload_native_project(project, DISTINCT_DEBUG_IDS, UPLOADS_PER_DEBUG_ID)
        upload_crash_sources(project, ELF_DEBUG_ID)

    def native_frames():
        return [
            {"instruction_addr": hex(ELF_IMAGE_ADDR + offset)}
            for offset in crash_frame_offsets("elf")
        ]

    def exception(type_, value, frames):
        return {
            "values": [
                {"type": type_, "value": value, "stacktrace": {"frames": frames}}
            ]
        }

    # Each builder returns a function that makes one event payload.
    def setup_plain():
        return lambda: {
            "platform": "python",
            "exception": exception(
                "ValueError",
                "bad input",
                [{"filename": "app/views.py", "function": "handle", "lineno": 42}],
            ),
        }

    def setup_js(code, sourcemap, debug_id, name, positions):
        upload_artifact_bundle(
            organization, project, debug_id_bundle(name, code, sourcemap, debug_id)
        )
        url = f"https://example.com/assets/{name}"
        return lambda: {
            "platform": "javascript",
            "debug_meta": {
                "images": [
                    {"type": "sourcemap", "code_file": url, "debug_id": debug_id}
                ]
            },
            "exception": exception(
                "Error",
                "boom",
                [
                    {"filename": url, "function": "?", "lineno": line, "colno": col}
                    for line, col in positions
                ],
            ),
        }

    def setup_js_small():
        code = (JS_FIXTURES / "app.min.js").read_bytes()
        sourcemap = (JS_FIXTURES / "app.min.js.map").read_bytes()
        debug_id = json.loads(sourcemap)["debugId"]
        return setup_js(
            code, sourcemap, debug_id, "app.min.js", [(1, 345), (1, 260), (1, 147)]
        )

    def setup_js_large():
        bundle = make_large_bundle()
        positions = [bundle.throw_positions[k] for k in [(0, 0), (80, 50), (159, 99)]]
        return setup_js(
            bundle.code, bundle.sourcemap, bundle.debug_id, "large.min.js", positions
        )

    def setup_native_debug_meta():
        upload_elf_and_sources()
        return lambda: {
            "platform": "native",
            "contexts": {"os": {"name": "Linux"}, "device": {"arch": "x86_64"}},
            "debug_meta": {
                "images": [
                    {
                        "type": "elf",
                        "debug_id": ELF_DEBUG_ID,
                        "image_addr": hex(ELF_IMAGE_ADDR),
                    }
                ]
            },
            "exception": exception("SIGSEGV", "Segfault", native_frames()),
        }

    def setup_native_no_debug_meta():
        upload_many_elfs()
        return lambda: {
            "platform": "native",
            "contexts": {"os": {"name": "Android"}, "device": {"arch": "x86_64"}},
            "exception": exception("SIGSEGV", "Segfault", native_frames()),
        }

    def setup_no_native_frames():
        upload_many_elfs()
        return lambda: {
            "platform": "python",
            "contexts": {"os": {"name": "Linux"}},
            "exception": exception(
                "ValueError",
                "bad input",
                [{"filename": "app/views.py", "function": "handle", "lineno": 42}],
            ),
        }

    def setup_proguard():
        mapping = (PROGUARD_FIXTURES / "mapping.txt").read_bytes()
        for index in range(PROGUARD_MAPPINGS):
            upload_proguard_mapping(
                project, mapping + f"# build {index}\n".encode(), str(uuid.uuid4())
            )
        return lambda: {
            "platform": "java",
            "contexts": {"os": {"name": "Android"}},
            "exception": exception(
                "IllegalStateException",
                "boom",
                [
                    {"module": "a.a.a", "function": "b", "lineno": 2},
                    {"module": "a.a.b", "function": "a", "lineno": 3},
                ],
            ),
        }

    make_payload = {
        "plain": setup_plain,
        "js_small": setup_js_small,
        "js_large": setup_js_large,
        "native_debug_meta": setup_native_debug_meta,
        "native_no_debug_meta": setup_native_no_debug_meta,
        "no_native_frames": setup_no_native_frames,
        "proguard": setup_proguard,
    }[scenario]()

    def process_one(fields=None):
        payload = {
            "event_id": uuid.uuid4().hex,
            "timestamp": timezone.now().isoformat(),
            "level": "error",
            **(fields or make_payload()),
        }
        message = IssueTaskMessage(
            project_id=project.id,
            organization_id=organization.id,
            received=timezone.now(),
            payload=ErrorIssueEventSchema(**payload),
        )
        run_async_closing(process_issue_events, [message])

    # Warm up imports and connections with an event that has no exception, so
    # nothing is symbolicated yet.
    process_one({"platform": "other", "message": "warm up"})

    # The first symbolicated event, where allocator growth shows up...
    rss_before = read_status_kib("VmRSS")
    reset_peak_rss()
    start = time.perf_counter()
    process_one()
    first_event_ms = (time.perf_counter() - start) * 1000
    first_peak = read_status_kib("VmHWM")

    # ...then steady state: the same kind of event, again and again.
    rss_steady = read_status_kib("VmRSS")
    reset_peak_rss()
    timings = []
    # Warm: keep the caches the first event loaded.
    with measure_symbolication(cold=False) as cost:
        for _ in range(events):
            start = time.perf_counter()
            process_one()
            timings.append(time.perf_counter() - start)
    peak = read_status_kib("VmHWM")

    return {
        "scenario": scenario,
        "events": events,
        "ms_per_event_mean": statistics.mean(timings) * 1000,
        "ms_per_event_p50": statistics.median(timings) * 1000,
        "ms_per_event_max": max(timings) * 1000,
        "first_event_ms": first_event_ms,
        "rss_before_mib": rss_before / 1024,
        "first_event_peak_growth_mib": (first_peak - rss_before) / 1024,
        "peak_rss_mib": peak / 1024,
        "steady_peak_growth_mib": (peak - rss_steady) / 1024,
        "files_opened_per_event": cost.files_opened / events,
        "mib_read_per_event": cost.bytes_read / events / 2**20,
        "sourcemap_parses_per_event": cost.sourcemap_parses / events,
        "resolver_attempts_per_event": len(cost.resolver_attempts) / events,
    }


# --- Parent: set up the database, run each scenario, report ----------------


def create_bench_database(keep: bool):
    use_test_settings()
    from django.db import connections

    from glitchtip.test_runner import TimedTestRunner

    connections["default"].settings_dict["TEST"]["NAME"] = BENCH_DB
    runner = TimedTestRunner(verbosity=0, keepdb=keep, interactive=False)
    # Migrations print progress on stdout; keep stdout for the results.
    with contextlib.redirect_stdout(sys.stderr):
        return runner, runner.setup_databases()


def print_table(results: list[dict]):
    columns = [
        ("scenario", 22, "{}", "scenario"),
        ("ms/ev p50", 10, "{:.1f}", "ms_per_event_p50"),
        ("ms/ev max", 10, "{:.1f}", "ms_per_event_max"),
        ("1st ms", 8, "{:.1f}", "first_event_ms"),
        ("1st +MiB", 9, "{:.1f}", "first_event_peak_growth_mib"),
        ("peak MiB", 9, "{:.0f}", "peak_rss_mib"),
        ("steady +MiB", 12, "{:.1f}", "steady_peak_growth_mib"),
        ("files/ev", 9, "{:.1f}", "files_opened_per_event"),
        ("MiB read/ev", 12, "{:.2f}", "mib_read_per_event"),
        ("parses/ev", 10, "{:.1f}", "sourcemap_parses_per_event"),
        ("difs tried/ev", 14, "{:.1f}", "resolver_attempts_per_event"),
    ]

    def row(cells):
        first, *rest = cells
        return f"{first:<{columns[0][1]}}" + "".join(
            f"{cell:>{width}}" for cell, (_, width, _, _) in zip(rest, columns[1:])
        )

    print(row([title for title, *_ in columns]))
    for result in results:
        print(row([fmt.format(result[key]) for _, _, fmt, key in columns]))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--events", type=int, default=20)
    parser.add_argument("--scenario", action="append", choices=SCENARIOS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--keepdb", action="store_true", help="Reuse and keep the scratch database."
    )
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.child:
        print(json.dumps(run_child(args.child, args.events)))
        return

    media_root = tempfile.mkdtemp(prefix="bench-symbolication-")
    os.environ["MEDIA_ROOT"] = media_root
    runner, old_config = create_bench_database(args.keepdb)
    results = []
    try:
        for scenario in args.scenario or SCENARIOS:
            completed = subprocess.run(
                [
                    sys.executable,
                    os.path.abspath(__file__),
                    "--child",
                    scenario,
                    "--events",
                    str(args.events),
                ],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "MEDIA_ROOT": media_root},
            )
            if completed.returncode:
                sys.stderr.write(completed.stderr)
                raise SystemExit(f"scenario {scenario} failed")
            results.append(json.loads(completed.stdout.strip().splitlines()[-1]))
            if not args.json:
                print(f"done: {scenario}", file=sys.stderr)
    finally:
        if not args.keepdb:
            runner.teardown_databases(old_config)
        shutil.rmtree(media_root, ignore_errors=True)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_table(results)


if __name__ == "__main__":
    main()
