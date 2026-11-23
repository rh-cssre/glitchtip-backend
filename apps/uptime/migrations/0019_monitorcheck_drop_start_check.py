"""Rebuild MonitorCheck: one timestamp (the UUIDv7 id), weekly partitions.

Two changes that both need the partition tree replaced, done together because
doing them separately would mean discarding check history twice.

**``start_check`` is dropped.** It duplicated information already carried by
``id``, and the two could disagree: ``id`` defaulted to insert time while
``start_check`` recorded probe time. Beyond removing the divergence, it makes
``id`` the sole temporal column -- and ``id`` is the RANGE partition key, so a
time-based query on this table can no longer be written without pruning
partitions.

**Range partitions become weekly.** The cost of a partitioned table is paid in
relation locks, and locks scale with the number of RANGE nodes rather than the
rows in them. One monitors page load takes a lock per partition it plans over.
Measured against a faithful reproduction:

    daily,  90-day retention, 4 buckets     1,946 locks
    daily,  90-day retention, 16 buckets    6,602 locks
    weekly, 90-day retention, any buckets     118 locks

A stock cluster's shared lock table is ``max_locks_per_transaction`` (64) x
``max_connections`` (100) = 6,400 entries, shared by every backend -- so on a
16-bucket install a single page load could exhaust it and fail unrelated
transactions, ingest included. Uptime check volume is orders of magnitude below
event volume, so daily granularity bought nothing except a finer retention
boundary; weekly means data may be kept up to 7 days past retention before its
partition is dropped.

Sequence, all outside a transaction and all idempotent:

1. Drop every existing partition, one per statement so locks never accumulate.
   This discards check history. It is deliberate: preserving a window would
   leave daily partitions in place that overlap the weekly ones created below,
   and the resulting mixed-granularity edge cases are exactly the sort of thing
   that turns an upgrade into a failed migration. Losing old check data is
   recoverable by waiting; a wedged migration is not.
2. Drop the column. With no partitions left this touches the parent alone -- a
   handful of locks rather than the 3,337 measured against a full tree. It also
   drops the two ``start_check`` indexes that depended on it.
3. Create the replacement indexes on the now-childless parent. Instant, and
   partitions created afterwards inherit them automatically, so none of the
   usual CONCURRENTLY-per-leaf machinery is needed.
4. Create weekly partitions covering the current window so ingest has somewhere
   to land immediately. ``maintain_partitions`` keeps them going from there.

**Alerting is unaffected.** ``cached_is_up``, ``cached_last_change`` and
``cached_consecutive_down`` live on ``uptime_monitor``, and ``_next_state``
reads only those -- ``perform_checks`` never consults ``MonitorCheck``. That
includes the post-pruning ``is_change`` baseline path.

**Do not run ``resync_monitor_cache`` after upgrading.** It rebuilds those
cached fields *from* check history, which this migration has just cleared.
"""

import logging
from datetime import UTC, datetime, timedelta

import apps.uptime.models as uptime_models
from django.db import migrations, models

logger = logging.getLogger(__name__)

TABLE = "uptime_monitorcheck"

INDEX_SPECS = (
    ("uptime_check_monitor_id_idx", "(monitor_id, id DESC)", ""),
    ("uptime_check_change_id_idx", "(monitor_id, id DESC)", " WHERE is_change"),
)


def _partitions(cursor, relname):
    """Immediate partitions of ``relname``.

    Relations are resolved by name through ``to_regclass`` rather than by
    passing oids as parameters: the Rust driver has no codec for the oid type
    in either direction. It also means search_path decides the schema, matching
    how the DDL below resolves.
    """
    cursor.execute(
        """
        SELECT c.relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = to_regclass(%s)
        ORDER BY c.relname
        """,
        [relname],
    )
    return [row[0] for row in cursor.fetchall()]


def _exists(cursor, relname):
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [relname])
    return cursor.fetchone()[0]


def forwards(apps, schema_editor):
    connection = schema_editor.connection
    if connection.in_atomic_block:
        raise RuntimeError(
            "This migration must run outside a transaction (atomic = False)."
        )

    from glitchtip.partition_manager import PartitionManager

    with connection.cursor() as cursor:
        if not _exists(cursor, TABLE):
            raise RuntimeError(
                f"{TABLE} does not exist. Apply the earlier uptime migrations first."
            )

        # 1 -- drop every partition, one statement at a time.
        existing = _partitions(cursor, TABLE)
        for name in existing:
            cursor.execute(f'DROP TABLE IF EXISTS "{name}" CASCADE')
        logger.info("Dropped %s monitor check partitions", len(existing))

        # 2 -- the column, and with it the indexes that referenced it.
        cursor.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN IF EXISTS start_check')

        # 3 -- replacement indexes on an empty parent; new partitions inherit.
        for index_name, columns, predicate in INDEX_SPECS:
            cursor.execute(
                f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                f'ON "{TABLE}" {columns}{predicate}'
            )

    # 4 -- weekly partitions so ingest has somewhere to land immediately.
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_week = today - timedelta(days=today.weekday())
    PartitionManager(connection).create_partitions_for_date_range(
        parent_table=TABLE,
        start_date=start_of_week,
        end_date=start_of_week + timedelta(weeks=3),
        partition_interval="WEEK",
        hash_buckets=None,
        hash_column="organization_id",
        key_type="uuid7",
    )


def backwards(apps, schema_editor):
    """Restore the column, empty. The original values are not recoverable."""
    with schema_editor.connection.cursor() as cursor:
        for index_name, _, _ in INDEX_SPECS:
            cursor.execute(f'DROP INDEX IF EXISTS "{index_name}"')
        cursor.execute(
            f'ALTER TABLE "{TABLE}" ADD COLUMN IF NOT EXISTS start_check '
            "TIMESTAMPTZ NOT NULL DEFAULT now()"
        )


class Migration(migrations.Migration):
    # Each partition drop must commit on its own so locks never accumulate.
    atomic = False

    dependencies = [
        ("uptime", "0018_monitor_cached_consecutive_down_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveIndex(
                    model_name="monitorcheck",
                    name="uptime_moni_monitor_a89b32_idx",
                ),
                migrations.RemoveIndex(
                    model_name="monitorcheck",
                    name="uptime_moni_monitor_b6d442_idx",
                ),
                migrations.RemoveField(
                    model_name="monitorcheck",
                    name="start_check",
                ),
                migrations.AlterModelOptions(
                    name="monitorcheck",
                    options={"ordering": ("-id",)},
                ),
                migrations.AlterField(
                    model_name="monitorcheck",
                    name="id",
                    field=models.UUIDField(
                        default=uptime_models._generate_uuid7,
                        editable=False,
                        help_text=(
                            "UUIDv7; its embedded timestamp is when the check started"
                        ),
                    ),
                ),
                migrations.AddIndex(
                    model_name="monitorcheck",
                    index=models.Index(
                        fields=["monitor", "-id"], name="uptime_check_monitor_id_idx"
                    ),
                ),
                migrations.AddIndex(
                    model_name="monitorcheck",
                    index=models.Index(
                        condition=models.Q(("is_change", True)),
                        fields=["monitor", "-id"],
                        name="uptime_check_change_id_idx",
                    ),
                ),
            ],
            database_operations=[migrations.RunPython(forwards, backwards)],
        ),
    ]
