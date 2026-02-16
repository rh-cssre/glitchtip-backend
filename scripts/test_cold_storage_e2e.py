"""
End-to-end test for issue event cold storage.

Run with minio + postgres up:
  docker compose run --rm \
    -e AWS_ACCESS_KEY_ID=minioadmin \
    -e AWS_SECRET_ACCESS_KEY=minioadmin \
    -e AWS_STORAGE_BUCKET_NAME=glitchtip-cold \
    -e AWS_S3_ENDPOINT_URL=http://minio:9000 \
    -e GLITCHTIP_ENABLE_DUCKDB=true \
    -e GLITCHTIP_EVENTS_HOT_DAYS=0 \
    web python manage.py shell < scripts/test_cold_storage_e2e.py

Or with filesystem cold storage:
  docker compose -f compose.yml -f compose.cold-volume.yml run --rm \
    -e GLITCHTIP_ENABLE_DUCKDB=true \
    -e GLITCHTIP_EVENTS_HOT_DAYS=0 \
    web python manage.py shell < scripts/test_cold_storage_e2e.py
"""

from datetime import datetime, timedelta, timezone

from apps.issue_events.cold_storage import (
    IssueEventRow,
    archive_and_swap_partition,
    get_event_from_cold,
    is_duckdb_available,
    query_cold_events,
)
from apps.issue_events.models import Issue, IssueEvent
from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from glitchtip.cold_storage import get_cold_storage_backend
from glitchtip.partition_manager import PartitionManager, UUID7Helper

print("=" * 60)
print("Issue Events Cold Storage E2E Test")
print("=" * 60)

# 1. Check DuckDB availability
assert is_duckdb_available(), "DuckDB not available — set GLITCHTIP_ENABLE_DUCKDB=true"
storage = get_cold_storage_backend()
assert storage, "No cold storage backend configured"
print(f"[OK] DuckDB available, storage backend={type(storage).__name__}")

# 2. Create test org + project + issue
org, _ = Organization.objects.get_or_create(name="cold-test-org", slug="cold-test-org")
project, _ = Project.objects.get_or_create(
    name="cold-test-project", slug="cold-test-project", organization=org
)
issue, _ = Issue.objects.get_or_create(
    project=project,
    title="Test Cold Storage Issue",
    defaults={"metadata": {"title": "Test Cold Storage Issue"}, "type": 0, "level": 4},
)
print(f"[OK] Created org={org.id}, project={project.id}, issue={issue.id}")

# 3. Create a partition for 2 days ago and insert events
target_date = datetime.now(timezone.utc).replace(
    hour=0, minute=0, second=0, microsecond=0
) - timedelta(days=2)
next_date = target_date + timedelta(days=1)
date_str = target_date.strftime("%Y%m%d")
partition_table = f"issue_events_issueevent_{date_str}"

manager = PartitionManager()
# Ensure partition exists
manager.create_partitions_for_date_range(
    parent_table="issue_events_issueevent",
    start_date=target_date,
    end_date=next_date,
    partition_interval="DAY",
    hash_buckets=None,
    hash_column="organization_id",
    key_type="uuid7",
)
print(f"[OK] Ensured partition {partition_table} exists")

# 4. Insert test events into the partition
event_ids = []
for i in range(5):
    t = target_date + timedelta(hours=i, minutes=30)
    eid = UUID7Helper.from_datetime(t)
    event_ids.append(eid)

    IssueEvent.objects.create(
        id=eid,
        event_id=None,
        timestamp=t,
        issue=issue,
        organization=org,
        release=None,
        type=0,
        level=4,
        title=f"Test event {i}",
        transaction=f"/api/test/{i}",
        data={"message": f"Test message {i}", "platform": "python"},
        tags={"environment": "test", "index": str(i)},
        hashes=[f"hash_{i}"],
    )

print(f"[OK] Inserted {len(event_ids)} events into partition")

# Verify they exist in hot storage
hot_count = IssueEvent.objects.filter(issue=issue).count()
print(f"[OK] Hot storage count: {hot_count}")
assert hot_count >= 5, f"Expected at least 5 events in hot, got {hot_count}"

# 5. Archive the partition to cold storage
print(f"\nArchiving partition {partition_table}...")
success = archive_and_swap_partition(partition_table)
assert success, "archive_and_swap_partition returned False"
print("[OK] Partition archived and dropped")

# Verify events are gone from hot storage
hot_count_after = IssueEvent.objects.filter(issue=issue).count()
print(f"[OK] Hot storage count after archival: {hot_count_after}")
assert hot_count_after == 0, (
    f"Expected 0 events in hot after archival, got {hot_count_after}"
)

# 6. Query cold storage — list query
print("\nQuerying cold storage (list)...")
cold_events = query_cold_events(
    organization_id=org.id,
    start_dt=target_date - timedelta(days=1),
    end_dt=next_date + timedelta(days=1),
    issue_id=issue.id,
    limit=100,
)
print(f"[OK] Cold storage returned {len(cold_events)} events")
assert len(cold_events) == 5, f"Expected 5 cold events, got {len(cold_events)}"

# Verify data integrity
for ce in cold_events:
    assert isinstance(ce, IssueEventRow), f"Expected IssueEventRow, got {type(ce)}"
    assert ce.organization_id == org.id
    assert ce.issue_id == issue.id
    assert ce.level == 4
    assert "Test" in ce.title
    assert ce.eventID  # Should return hex string
    assert ce.received  # Should derive from UUIDv7
    assert ce.message  # Should come from data or title
    assert ce.tags.get("environment") == "test"
    assert len(ce.hashes) == 1

print("[OK] Data integrity verified for all cold events")

# 7. Single event lookup from cold
print("\nTesting single event lookup from cold...")
single = get_event_from_cold(
    organization_id=org.id,
    event_id=event_ids[0],
    event_time=target_date + timedelta(hours=0, minutes=30),
)
assert single is not None, "Single event lookup returned None"
assert single.id == event_ids[0]
print(f"[OK] Single event lookup: id={single.id}, title={single.title}")

# 8. Test IssueEventRow properties for schema compatibility
print("\nVerifying schema-compatible properties...")
print(f"  eventID: {single.eventID}")
print(f"  received: {single.received}")
print(f"  message: {single.message}")
print(f"  metadata: {single.metadata}")
print(f"  platform: {single.platform}")
print(f"  get_type_display: {single.get_type_display()}")
print(f"  get_level_display: {single.get_level_display()}")
print("[OK] All IssueEventRow properties work")

# 9. Cleanup
Issue.objects.filter(id=issue.id).delete()
Project.objects.filter(id=project.id).delete()
Organization.objects.filter(id=org.id).delete()
print("\n[OK] Test data cleaned up")

print("\n" + "=" * 60)
print("ALL E2E TESTS PASSED")
print("=" * 60)
