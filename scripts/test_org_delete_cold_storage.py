"""
Manual E2E test: verify cold storage cleanup on org/project deletion.

Creates an org with two projects, inserts issue events + logs into partitions
for both projects, archives them to cold storage, then:
1. Deletes project_1 and verifies its data is removed from Parquet files
   while project_2's data remains intact.
2. Deletes the org and verifies all cold storage files are removed.

Run with minio + postgres up:
  docker compose -f compose.yml -f compose.minio.yml up -d
  docker compose -f compose.yml -f compose.minio.yml run --rm \
    -e GLITCHTIP_ENABLE_DUCKDB=true \
    -e GLITCHTIP_EVENTS_HOT_DAYS=0 \
    web python manage.py shell < scripts/test_org_delete_cold_storage.py

Or with filesystem cold storage:
  docker compose -f compose.yml -f compose.cold-volume.yml up -d
  docker compose -f compose.yml -f compose.cold-volume.yml run --rm \
    -e GLITCHTIP_ENABLE_DUCKDB=true \
    -e GLITCHTIP_EVENTS_HOT_DAYS=0 \
    web python manage.py shell < scripts/test_org_delete_cold_storage.py
"""

from datetime import datetime, timedelta, timezone

from django.db import models

from apps.issue_events.cold_storage import (
    ISSUE_EVENT_EXPORT_COLUMN_TYPES,
    ISSUE_EVENT_SELECT_SQL,
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

print("=" * 70)
print("Org/Project Deletion Cold Storage E2E Test")
print("=" * 70)

# ── 0. Pre-checks ────────────────────────────────────────────────────
assert is_duckdb_available(), "DuckDB not available — set GLITCHTIP_ENABLE_DUCKDB=true"
storage = get_cold_storage_backend()
assert storage, "No storage backend"
print(f"[OK] DuckDB available, storage backend={type(storage).__name__}")

# ── 1. Create test org + 2 projects + issues ─────────────────────────
org, _ = Organization.objects.get_or_create(
    name="delete-test-org", slug="delete-test-org"
)
proj1, _ = Project.objects.get_or_create(
    name="proj-delete-1", slug="proj-delete-1", organization=org
)
proj2, _ = Project.objects.get_or_create(
    name="proj-keep-2", slug="proj-keep-2", organization=org
)
issue1, _ = Issue.objects.get_or_create(
    project=proj1,
    title="Issue in project 1",
    defaults={"metadata": {"title": "Issue in project 1"}, "type": 0, "level": 4},
)
issue2, _ = Issue.objects.get_or_create(
    project=proj2,
    title="Issue in project 2",
    defaults={"metadata": {"title": "Issue in project 2"}, "type": 0, "level": 4},
)
print(f"[OK] org={org.id}, proj1={proj1.id}, proj2={proj2.id}")
print(f"     issue1={issue1.id}, issue2={issue2.id}")

# ── 2. Create partitions for 2 days ago ──────────────────────────────
target_date = datetime.now(timezone.utc).replace(
    hour=0, minute=0, second=0, microsecond=0
) - timedelta(days=2)
next_date = target_date + timedelta(days=1)
date_str = target_date.strftime("%Y%m%d")

manager = PartitionManager()

# Issue events partition
manager.create_partitions_for_date_range(
    parent_table="issue_events_issueevent",
    start_date=target_date,
    end_date=next_date,
    partition_interval="DAY",
    hash_buckets=None,
    hash_column="organization_id",
    key_type="uuid7",
)

# Logs partition
manager.create_partitions_for_date_range(
    parent_table="logs_logevent",
    start_date=target_date,
    end_date=next_date,
    partition_interval="DAY",
    hash_buckets=None,
    hash_column="organization_id",
    key_type="uuid7",
)
print(f"[OK] Partitions created for {date_str}")

# ── 3. Insert test data ──────────────────────────────────────────────
# Issue events: 3 for proj1, 3 for proj2
for i in range(3):
    t = target_date + timedelta(hours=i, minutes=10)
    IssueEvent.objects.create(
        id=UUID7Helper.from_datetime(t),
        timestamp=t,
        issue=issue1,
        organization=org,
        type=0,
        level=4,
        title=f"Proj1 event {i}",
        transaction=f"/api/proj1/{i}",
        data={"message": f"Proj1 msg {i}", "platform": "python"},
        tags={"project": "proj1"},
        hashes=[f"h1_{i}"],
    )

for i in range(3):
    t = target_date + timedelta(hours=i + 5, minutes=10)
    IssueEvent.objects.create(
        id=UUID7Helper.from_datetime(t),
        timestamp=t,
        issue=issue2,
        organization=org,
        type=0,
        level=4,
        title=f"Proj2 event {i}",
        transaction=f"/api/proj2/{i}",
        data={"message": f"Proj2 msg {i}", "platform": "python"},
        tags={"project": "proj2"},
        hashes=[f"h2_{i}"],
    )

# Logs: 3 for proj1, 3 for proj2
for i in range(3):
    t = target_date + timedelta(hours=i, minutes=20)
    LogEvent.objects.create(
        id=UUID7Helper.from_datetime(t),
        organization=org,
        project=proj1,
        level=2,  # INFO
        body=f"Proj1 log {i}",
        service="svc-proj1",
        environment="test",
        data={},
    )

for i in range(3):
    t = target_date + timedelta(hours=i + 5, minutes=20)
    LogEvent.objects.create(
        id=UUID7Helper.from_datetime(t),
        organization=org,
        project=proj2,
        level=2,  # INFO
        body=f"Proj2 log {i}",
        service="svc-proj2",
        environment="test",
        data={},
    )

print("[OK] Inserted 6 issue events + 6 logs (3 per project)")

# ── 4. Archive both partitions to cold storage ───────────────────────
ie_partition = f"issue_events_issueevent_{date_str}"
log_partition = f"logs_logevent_{date_str}"

print(f"\nArchiving {ie_partition}...")
ok = archive_and_swap_partition(
    ie_partition,
    "issue_events_issueevent",
    ISSUE_EVENT_EXPORT_COLUMN_TYPES,
    ISSUE_EVENT_SELECT_SQL,
)
assert ok, "Issue event archival failed"
print("[OK] Issue events archived")

print(f"Archiving {log_partition}...")
ok = archive_and_swap_partition(
    log_partition,
    "logs_logevent",
    LOG_COLUMN_TYPES,
    LOGS_SELECT_SQL,
)
assert ok, "Log archival failed"
print("[OK] Logs archived")

# ── 5. Verify cold storage files exist ───────────────────────────────
ie_path = get_org_cold_storage_path("issue_events_issueevent", org.id, date_str)
log_path = get_org_cold_storage_path("logs_logevent", org.id, date_str)

assert storage.exists(ie_path), f"Issue event Parquet not found: {ie_path}"
assert storage.exists(log_path), f"Log Parquet not found: {log_path}"
print("\n[OK] Cold files exist:")
print(f"     {ie_path}")
print(f"     {log_path}")

# Verify cold query returns all 6 issue events
cold_events = query_cold_events(
    organization_id=org.id,
    start_dt=target_date - timedelta(days=1),
    end_dt=next_date + timedelta(days=1),
    limit=100,
)
assert len(cold_events) == 6, f"Expected 6 cold issue events, got {len(cold_events)}"
print(f"[OK] Cold query returns {len(cold_events)} issue events")

# ── 6. TEST: Delete project 1 — rewrite Parquet files ────────────────
print("\n" + "-" * 70)
print("TEST 1: Delete project 1 (rewrite Parquet, keep project 2's data)")
print("-" * 70)

# Rewrite logs (filter by project_id)
rewritten = rewrite_parquet_excluding_project(
    org_id=org.id,
    project_id=proj1.id,
    table_name="logs_logevent",
    column_types=LOG_COLUMN_TYPES,
)
print(f"[OK] Rewrote {rewritten} log file(s)")

# Rewrite issue events (filter by issue_id)
issue1_ids = list(Issue.objects.filter(project=proj1).values_list("id", flat=True))
print(f"     issue1 IDs to exclude: {issue1_ids}")
rewritten = rewrite_parquet_excluding_project(
    org_id=org.id,
    issue_ids=issue1_ids,
    table_name="issue_events_issueevent",
    column_types=ISSUE_EVENT_EXPORT_COLUMN_TYPES,
)
print(f"[OK] Rewrote {rewritten} issue event file(s)")

# Verify: cold issue events should now only have proj2's 3 events
cold_events_after = query_cold_events(
    organization_id=org.id,
    start_dt=target_date - timedelta(days=1),
    end_dt=next_date + timedelta(days=1),
    limit=100,
)
print(f"[CHECK] Cold issue events after proj1 delete: {len(cold_events_after)}")
assert len(cold_events_after) == 3, (
    f"Expected 3 cold issue events (proj2 only), got {len(cold_events_after)}"
)
for ce in cold_events_after:
    assert ce.issue_id == issue2.id, (
        f"Event {ce.id} has issue_id={ce.issue_id}, expected {issue2.id}"
    )
print("[OK] Only project 2's issue events remain in cold storage")

# Verify: log Parquet still exists but only has proj2's data
# (We can verify via DuckDB query)
log_relative_path = get_org_cold_storage_path("logs_logevent", org.id, date_str)
log_parquet_path = get_duckdb_parquet_path(storage, log_relative_path)
duck = get_duckdb_connection(storage)
try:
    result = duck.execute(
        f"SELECT project_id, COUNT(*) FROM read_parquet('{log_parquet_path}') GROUP BY project_id"
    ).fetchall()
    print(f"[CHECK] Log Parquet project breakdown: {result}")
    assert len(result) == 1, f"Expected 1 project in logs, got {len(result)}"
    assert result[0][0] == proj2.id, f"Expected proj2 ({proj2.id}), got {result[0][0]}"
    assert result[0][1] == 3, f"Expected 3 logs, got {result[0][1]}"
    print("[OK] Only project 2's logs remain in cold storage")
finally:
    duck.close()

# ── 7. TEST: Delete the org — delete all cold storage files ──────────
print("\n" + "-" * 70)
print("TEST 2: Delete org (remove all cold storage files)")
print("-" * 70)

# Delete cold storage for both tables
for table in ["issue_events_issueevent", "logs_logevent"]:
    deleted = delete_org_cold_storage(org.id, table)
    print(f"[OK] Deleted {deleted} cold file(s) for {table}")

# Verify files are gone
assert not storage.exists(ie_path), f"Issue event Parquet still exists: {ie_path}"
assert not storage.exists(log_path), f"Log Parquet still exists: {log_path}"
print("[OK] All cold storage files deleted")

# Verify cold query returns empty
cold_events_final = query_cold_events(
    organization_id=org.id,
    start_dt=target_date - timedelta(days=1),
    end_dt=next_date + timedelta(days=1),
    limit=100,
)
assert len(cold_events_final) == 0, (
    f"Expected 0 cold events after org delete, got {len(cold_events_final)}"
)
print("[OK] Cold query returns 0 events after org deletion")

# ── 8. Cleanup DB rows ───────────────────────────────────────────────
Issue.objects.filter(id__in=[issue1.id, issue2.id]).delete()
# Use super().delete() to bypass soft-delete
models.Model.delete(proj1)
models.Model.delete(proj2)
org.force_delete()
print("[OK] Test data cleaned up from DB")

print("\n" + "=" * 70)
print("ALL TESTS PASSED")
print("=" * 70)
