"""
Manual E2E test for cold storage — works with both S3 (MinIO) and filesystem backends.

Tests:
1. Verify backend detection and DuckDB availability
2. Create test data (org, projects, issue events, logs)
3. Create partitions, insert data, archive to cold storage
4. Query cold storage (list + single event)
5. Rewrite Parquet excluding a project
6. Delete org cold storage
7. Cleanup
"""
import sys
from datetime import datetime, timedelta, timezone

import django

django.setup()

from django.db import models

from apps.issue_events.cold_storage import (
    ISSUE_EVENT_EXPORT_COLUMN_TYPES,
    ISSUE_EVENT_SELECT_SQL,
    archive_and_swap_partition as ie_archive_and_swap,
    get_event_from_cold,
    is_duckdb_available,
    query_cold_events,
)
from apps.issue_events.models import Issue, IssueEvent
from apps.logs.cold_storage import EXPORT_COLUMN_TYPES as LOG_COLUMN_TYPES
from apps.logs.cold_storage import LOGS_SELECT_SQL
from apps.logs.models import LogEvent
from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from glitchtip.cold_storage import (
    archive_and_swap_partition,
    delete_org_cold_storage,
    get_cold_storage_backend,
    get_duckdb_connection,
    get_duckdb_parquet_path,
    get_org_cold_storage_path,
    rewrite_parquet_excluding_project,
)
from glitchtip.partition_manager import PartitionManager, UUID7Helper

PASS = 0
FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} — {detail}")


print("=" * 70)
print("Cold Storage Manual E2E Test")
print("=" * 70)

# ── 1. Backend detection ─────────────────────────────────────────────
print("\n--- 1. Backend detection ---")
storage = get_cold_storage_backend()
check("get_cold_storage_backend() returns non-None", storage is not None)
check("is_duckdb_available() returns True", is_duckdb_available())

backend_type = type(storage).__name__
print(f"  Backend type: {backend_type}")

from glitchtip.cold_storage import _is_s3_storage

is_s3 = _is_s3_storage(storage)
if is_s3:
    print(f"  S3 bucket: {storage.bucket_name}")
    print(f"  S3 endpoint: {getattr(storage, 'endpoint_url', 'default')}")
else:
    print(f"  Filesystem location: {storage.location}")

# ── 2. DuckDB connection ─────────────────────────────────────────────
print("\n--- 2. DuckDB connection ---")
duck = get_duckdb_connection(storage)
try:
    result = duck.execute("SELECT 1 + 1").fetchone()
    check("DuckDB connection works", result[0] == 2)

    if is_s3:
        # Verify httpfs is loaded for S3
        exts = duck.execute("SELECT extension_name, loaded FROM duckdb_extensions() WHERE loaded = true").fetchall()
        ext_names = [e[0] for e in exts]
        check("httpfs extension loaded (S3)", "httpfs" in ext_names)
    else:
        # Verify httpfs is NOT loaded for filesystem
        exts = duck.execute("SELECT extension_name, loaded FROM duckdb_extensions() WHERE loaded = true").fetchall()
        ext_names = [e[0] for e in exts]
        check("httpfs extension NOT loaded (filesystem)", "httpfs" not in ext_names,
              f"loaded extensions: {ext_names}")
finally:
    duck.close()

# ── 3. Create test data ──────────────────────────────────────────────
print("\n--- 3. Create test data ---")
org, _ = Organization.objects.get_or_create(
    name="manual-test-org", slug="manual-test-org"
)
proj1, _ = Project.objects.get_or_create(
    name="manual-proj1", slug="manual-proj1", organization=org
)
proj2, _ = Project.objects.get_or_create(
    name="manual-proj2", slug="manual-proj2", organization=org
)
issue1, _ = Issue.objects.get_or_create(
    project=proj1, title="Issue proj1",
    defaults={"metadata": {"title": "Issue proj1"}, "type": 0, "level": 4},
)
issue2, _ = Issue.objects.get_or_create(
    project=proj2, title="Issue proj2",
    defaults={"metadata": {"title": "Issue proj2"}, "type": 0, "level": 4},
)
print(f"  org={org.id}, proj1={proj1.id}, proj2={proj2.id}")
print(f"  issue1={issue1.id}, issue2={issue2.id}")
check("Test objects created", True)

# ── 4. Create partitions and insert events ────────────────────────────
print("\n--- 4. Partitions and data insertion ---")
target_date = datetime.now(timezone.utc).replace(
    hour=0, minute=0, second=0, microsecond=0
) - timedelta(days=2)
next_date = target_date + timedelta(days=1)
date_str = target_date.strftime("%Y%m%d")

manager = PartitionManager()
for table in ["issue_events_issueevent", "logs_logevent"]:
    manager.create_partitions_for_date_range(
        parent_table=table,
        start_date=target_date,
        end_date=next_date,
        partition_interval="DAY",
        hash_buckets=None,
        hash_column="organization_id",
        key_type="uuid7",
    )
print(f"  Partitions created for {date_str}")

# Issue events: 3 per project
ie_ids = []
for i in range(3):
    t = target_date + timedelta(hours=i, minutes=10)
    eid = UUID7Helper.from_datetime(t)
    ie_ids.append(eid)
    IssueEvent.objects.create(
        id=eid, timestamp=t, issue=issue1, organization=org,
        type=0, level=4, title=f"P1 event {i}",
        transaction=f"/api/p1/{i}",
        data={"message": f"P1 msg {i}", "platform": "python"},
        tags={"project": "proj1"}, hashes=[f"h1_{i}"],
    )
for i in range(3):
    t = target_date + timedelta(hours=i + 5, minutes=10)
    IssueEvent.objects.create(
        id=UUID7Helper.from_datetime(t), timestamp=t,
        issue=issue2, organization=org,
        type=0, level=4, title=f"P2 event {i}",
        transaction=f"/api/p2/{i}",
        data={"message": f"P2 msg {i}", "platform": "python"},
        tags={"project": "proj2"}, hashes=[f"h2_{i}"],
    )

# Logs: 3 per project
for i in range(3):
    t = target_date + timedelta(hours=i, minutes=20)
    LogEvent.objects.create(
        id=UUID7Helper.from_datetime(t), organization=org, project=proj1,
        level=2, body=f"P1 log {i}", service="svc-p1", environment="test", data={},
    )
for i in range(3):
    t = target_date + timedelta(hours=i + 5, minutes=20)
    LogEvent.objects.create(
        id=UUID7Helper.from_datetime(t), organization=org, project=proj2,
        level=2, body=f"P2 log {i}", service="svc-p2", environment="test", data={},
    )

hot_ie = IssueEvent.objects.filter(organization=org).count()
hot_log = LogEvent.objects.filter(organization=org).count()
check("6 issue events in hot storage", hot_ie == 6, f"got {hot_ie}")
check("6 logs in hot storage", hot_log == 6, f"got {hot_log}")

# ── 5. Archive partitions ────────────────────────────────────────────
print("\n--- 5. Archive partitions to cold storage ---")
ie_partition = f"issue_events_issueevent_{date_str}"
log_partition = f"logs_logevent_{date_str}"

ok_ie = archive_and_swap_partition(
    ie_partition, "issue_events_issueevent",
    ISSUE_EVENT_EXPORT_COLUMN_TYPES, ISSUE_EVENT_SELECT_SQL,
)
check("Issue events archived", ok_ie)

ok_log = archive_and_swap_partition(
    log_partition, "logs_logevent",
    LOG_COLUMN_TYPES, LOGS_SELECT_SQL,
)
check("Logs archived", ok_log)

# Verify hot storage is empty
hot_ie_after = IssueEvent.objects.filter(organization=org).count()
hot_log_after = LogEvent.objects.filter(organization=org).count()
check("0 issue events in hot after archival", hot_ie_after == 0, f"got {hot_ie_after}")
check("0 logs in hot after archival", hot_log_after == 0, f"got {hot_log_after}")

# ── 6. Verify Parquet files exist ────────────────────────────────────
print("\n--- 6. Verify Parquet files on storage backend ---")
ie_path = get_org_cold_storage_path("issue_events_issueevent", org.id, date_str)
log_path = get_org_cold_storage_path("logs_logevent", org.id, date_str)

check("Issue event Parquet exists", storage.exists(ie_path), ie_path)
check("Log Parquet exists", storage.exists(log_path), log_path)

# Check DuckDB can read them
ie_parquet = get_duckdb_parquet_path(storage, ie_path)
log_parquet = get_duckdb_parquet_path(storage, log_path)
print(f"  IE parquet path: {ie_parquet}")
print(f"  Log parquet path: {log_parquet}")

duck = get_duckdb_connection(storage)
try:
    ie_count = duck.execute(f"SELECT COUNT(*) FROM read_parquet('{ie_parquet}')").fetchone()[0]
    log_count = duck.execute(f"SELECT COUNT(*) FROM read_parquet('{log_parquet}')").fetchone()[0]
    check("DuckDB reads 6 issue events from Parquet", ie_count == 6, f"got {ie_count}")
    check("DuckDB reads 6 logs from Parquet", log_count == 6, f"got {log_count}")

    # Show schema
    ie_cols = duck.execute(f"DESCRIBE SELECT * FROM read_parquet('{ie_parquet}')").fetchall()
    print(f"  IE Parquet columns: {[c[0] for c in ie_cols]}")
    log_cols = duck.execute(f"DESCRIBE SELECT * FROM read_parquet('{log_parquet}')").fetchall()
    print(f"  Log Parquet columns: {[c[0] for c in log_cols]}")
finally:
    duck.close()

# ── 7. Query cold storage via application layer ──────────────────────
print("\n--- 7. Query cold storage (application layer) ---")
cold_events = query_cold_events(
    organization_id=org.id,
    start_dt=target_date - timedelta(days=1),
    end_dt=next_date + timedelta(days=1),
    limit=100,
)
check("query_cold_events returns 6", len(cold_events) == 6, f"got {len(cold_events)}")

# Filter by issue
cold_p1 = query_cold_events(
    organization_id=org.id,
    start_dt=target_date - timedelta(days=1),
    end_dt=next_date + timedelta(days=1),
    issue_id=issue1.id,
    limit=100,
)
check("query_cold_events(issue=p1) returns 3", len(cold_p1) == 3, f"got {len(cold_p1)}")

# Verify data integrity
for ce in cold_events:
    check(f"  Event {ce.id} has org_id={org.id}", ce.organization_id == org.id)

# Single event lookup
print("\n--- 7b. Single event lookup ---")
single = get_event_from_cold(
    organization_id=org.id,
    event_id=ie_ids[0],
    event_time=target_date + timedelta(hours=0, minutes=10),
)
check("get_event_from_cold returns event", single is not None)
if single:
    check("Event ID matches", single.id == ie_ids[0])
    check("Event has tags", single.tags.get("project") == "proj1")
    check("Event has hashes", len(single.hashes) == 1)
    check("Event has eventID property", bool(single.eventID))
    check("Event has received property", single.received is not None)
    check("Event has message property", bool(single.message))

# ── 8. Rewrite Parquet excluding project 1 ───────────────────────────
print("\n--- 8. Rewrite Parquet excluding project 1 ---")

# Rewrite logs (filter by project_id)
rewritten_logs = rewrite_parquet_excluding_project(
    org_id=org.id,
    project_id=proj1.id,
    table_name="logs_logevent",
    column_types=LOG_COLUMN_TYPES,
)
check("Rewrote log Parquet", rewritten_logs > 0, f"rewritten={rewritten_logs}")

# Rewrite issue events (filter by issue_ids)
issue1_ids = list(Issue.objects.filter(project=proj1).values_list("id", flat=True))
rewritten_ie = rewrite_parquet_excluding_project(
    org_id=org.id,
    issue_ids=issue1_ids,
    table_name="issue_events_issueevent",
    column_types=ISSUE_EVENT_EXPORT_COLUMN_TYPES,
)
check("Rewrote IE Parquet", rewritten_ie > 0, f"rewritten={rewritten_ie}")

# Verify only proj2 data remains
cold_after = query_cold_events(
    organization_id=org.id,
    start_dt=target_date - timedelta(days=1),
    end_dt=next_date + timedelta(days=1),
    limit=100,
)
check("3 issue events remain after rewrite", len(cold_after) == 3, f"got {len(cold_after)}")
for ce in cold_after:
    check(f"  Event {ce.id} belongs to issue2", ce.issue_id == issue2.id,
          f"issue_id={ce.issue_id}")

# Verify log Parquet only has proj2
duck = get_duckdb_connection(storage)
try:
    log_parquet = get_duckdb_parquet_path(storage, log_path)
    rows = duck.execute(
        f"SELECT project_id, COUNT(*) FROM read_parquet('{log_parquet}') GROUP BY project_id"
    ).fetchall()
    check("Log Parquet has 1 project", len(rows) == 1, f"got {len(rows)} projects")
    if rows:
        check("Log Parquet project is proj2", rows[0][0] == proj2.id,
              f"got project_id={rows[0][0]}")
        check("Log Parquet has 3 rows", rows[0][1] == 3, f"got {rows[0][1]}")
finally:
    duck.close()

# ── 9. Delete org cold storage ────────────────────────────────────────
print("\n--- 9. Delete org cold storage ---")
for table in ["issue_events_issueevent", "logs_logevent"]:
    deleted = delete_org_cold_storage(org.id, table)
    check(f"Deleted cold files for {table}", deleted > 0, f"deleted={deleted}")

check("IE Parquet gone", not storage.exists(ie_path))
check("Log Parquet gone", not storage.exists(log_path))

cold_final = query_cold_events(
    organization_id=org.id,
    start_dt=target_date - timedelta(days=1),
    end_dt=next_date + timedelta(days=1),
    limit=100,
)
check("0 events after org delete", len(cold_final) == 0, f"got {len(cold_final)}")

# ── 10. Cleanup DB ───────────────────────────────────────────────────
print("\n--- 10. Cleanup ---")
Issue.objects.filter(id__in=[issue1.id, issue2.id]).delete()
models.Model.delete(proj1)
models.Model.delete(proj2)
org.force_delete()
print("  DB objects cleaned up")

# ── Summary ──────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print(f"RESULTS: {PASS} passed, {FAIL} failed")
if FAIL:
    print("SOME TESTS FAILED")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
print("=" * 70)
