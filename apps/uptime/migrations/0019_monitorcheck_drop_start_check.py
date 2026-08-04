"""Make the UUIDv7 ``id`` the only timestamp on MonitorCheck.

``start_check`` duplicated information already carried by ``id``, and the two
could disagree: ``id`` defaulted to insert time while ``start_check`` recorded
probe time. Dropping the column removes the divergence and, more usefully,
makes ``id`` the sole temporal column -- which is also the RANGE partition key,
so a time-based query on this table can no longer be written without pruning
partitions.

Three phases, in this order, all idempotent and all outside a transaction.

1. **Discard check history older than PRESERVE_DAYS.** This is not
   housekeeping, it is what makes the rest of the migration safe. ``ALTER TABLE
   ... DROP COLUMN`` recurses through the whole partition tree and takes one
   lock per relation in a single transaction. Measured on a faithful
   reproduction of a large install (98 daily partitions x 16 hash buckets =
   1,568 leaves) that is **3,337 locks** -- over half of a default cluster's
   entire shared lock table (``max_locks_per_transaction`` 64 x
   ``max_connections`` 100 = 6,400), which is shared with every other backend.
   Trimming to a one-week window first brings it to **243**. At the default 4
   hash buckets the same numbers are 985 and 75.

   Dropping whole partitions is cheap (metadata only), is done one partition
   per statement so locks never accumulate, and is the same operation retention
   performs routinely. The cost is history: on upgrade, checks older than a
   week are gone rather than aging out over the retention window. Monitor
   *state* is unaffected -- ``cached_is_up``, ``cached_last_change`` and
   ``cached_consecutive_down`` live on ``uptime_monitor``, and ``_next_state``
   reads only those, so alerting cannot be perturbed by this.

2. **Drop the column.** Metadata only; Postgres marks it dropped rather than
   rewriting rows. This also drops the two ``start_check`` indexes that
   depended on it, so they need no separate migration.

3. **Build the replacement indexes**, one leaf at a time with CONCURRENTLY,
   adopted into a partitioned index via ``ON ONLY`` + ``ATTACH PARTITION``.
   Postgres refuses CONCURRENTLY on a partitioned parent but allows it per
   leaf, so nothing takes a long exclusive lock. Doing this after phase 1 means
   building over ~112 leaves instead of ~1,568.

Re-running is safe and is the intended recovery from any interruption: dropped
partitions stay dropped, the column drop is ``IF EXISTS``, and index building
skips leaves that already have a valid index while replacing any left INVALID
by a killed build. Phase 2 refuses to run rather than risk exhausting the lock
table, with an error saying exactly what to do.

**Do not run ``resync_monitor_cache`` after this.** It rebuilds
``cached_is_up``/``cached_last_change`` *from* check history, so running it
against the trimmed table would discard monitor state this migration preserves.
"""

import logging

import apps.uptime.models as apps_models
from django.db import migrations, models

logger = logging.getLogger(__name__)

TABLE = "uptime_monitorcheck"

# Check history retained through the upgrade. A week is well past the point
# anyone reads an individual check, and it keeps the lock count for phase 2 an
# order of magnitude below the cluster's budget.
PRESERVE_DAYS = 7

# Refuse phase 2 above this many relations in the tree. Chosen well under a
# default cluster's 6,400-entry shared lock table, since that table is shared
# with every other backend and a migration must not be the thing that exhausts
# it. Roughly 2.5 locks are taken per leaf (leaf table, its indexes, and the
# enclosing partitioned index).
MAX_TREE_RELATIONS = 800

# (parent index name, child suffix, columns, predicate)
INDEX_SPECS = (
    ("uptime_check_monitor_id_idx", "mid", "(monitor_id, id DESC)", ""),
    ("uptime_check_change_id_idx", "cid", "(monitor_id, id DESC)", " WHERE is_change"),
)


def _children(cursor, relname):
    """Immediate partitions of ``relname`` as (name, relkind) pairs.

    Relations are resolved by name through ``to_regclass`` rather than by
    passing oids as query parameters -- the Rust driver has no codec for the
    oid type in either direction -- which also means the search_path decides
    the schema, matching how the DDL below resolves.
    """
    cursor.execute(
        """
        SELECT c.relname, c.relkind
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = to_regclass(%s)
        ORDER BY c.relname
        """,
        [relname],
    )
    return cursor.fetchall()


def _attached_index(cursor, parent_index, child_table):
    """Index already attached to ``parent_index`` covering ``child_table``.

    Keyed on the child *table*, not on a computed index name: when a partition
    is created while the parent index exists, Postgres clones the index onto it
    under a name of its own choosing and attaches it. Looking for our own name
    would miss that and then fail the ATTACH with "another index is already
    attached".
    """
    cursor.execute(
        """
        SELECT c.relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_index x ON x.indexrelid = c.oid
        WHERE i.inhparent = to_regclass(%s) AND x.indrelid = to_regclass(%s)
        """,
        [parent_index, child_table],
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _exists(cursor, relname):
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [relname])
    return cursor.fetchone()[0]


def _index_valid(cursor, name):
    """None when absent, else indisvalid."""
    cursor.execute(
        """
        SELECT i.indisvalid
        FROM pg_class c
        JOIN pg_index i ON i.indexrelid = c.oid
        WHERE c.oid = to_regclass(%s)
        """,
        [name],
    )
    row = cursor.fetchone()
    return None if row is None else row[0]


def _child_index_name(child_relname, suffix):
    # Postgres caps identifiers at 63 bytes.
    return f"{child_relname[: 63 - len(suffix) - 1]}_{suffix}"


def _build_index(
    cursor, relname, relkind, index_name, columns, predicate, suffix, counts
):
    if relkind == "p":
        if _index_valid(cursor, index_name) is None:
            cursor.execute(
                f'CREATE INDEX "{index_name}" ON ONLY "{relname}" {columns}{predicate}'
            )
        for child_name, child_kind in _children(cursor, relname):
            if _attached_index(cursor, index_name, child_name):
                counts["skipped"] += 1
                continue
            child_index = _child_index_name(child_name, suffix)
            _build_index(
                cursor,
                child_name,
                child_kind,
                child_index,
                columns,
                predicate,
                suffix,
                counts,
            )
            cursor.execute(
                f'ALTER INDEX "{index_name}" ATTACH PARTITION "{child_index}"'
            )
        return

    valid = _index_valid(cursor, index_name)
    if valid is False:
        # Left behind by an interrupted build; it can never become valid.
        cursor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{index_name}"')
        valid = None
    if valid is None:
        cursor.execute(
            f'CREATE INDEX CONCURRENTLY "{index_name}" ON "{relname}" '
            f"{columns}{predicate}"
        )
        counts["built"] += 1
    else:
        counts["skipped"] += 1


def _count_tree_relations(cursor, relname):
    cursor.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT to_regclass(%s) AS oid
            UNION ALL
            SELECT i.inhrelid FROM pg_inherits i JOIN tree t ON i.inhparent = t.oid
        )
        SELECT count(*) FROM tree
        """,
        [relname],
    )
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

        # Phase 1 -- trim to the preserve window so phases 2 and 3 stay small.
        dropped = PartitionManager(connection).drop_old_partitions(TABLE, PRESERVE_DAYS)
        logger.info(
            "Dropped %s monitor check partitions older than %s days",
            dropped,
            PRESERVE_DAYS,
        )

        # Phase 2 -- drop the column, which also drops its two indexes.
        relations = _count_tree_relations(cursor, TABLE)
        if relations > MAX_TREE_RELATIONS:
            raise RuntimeError(
                f"{TABLE} still has {relations} partitions after trimming to "
                f"{PRESERVE_DAYS} days, and dropping a column locks every one of "
                "them in a single transaction. Refusing to proceed rather than "
                "risk exhausting the cluster lock table.\n"
                "Either raise max_locks_per_transaction (a value giving "
                f"max_locks_per_transaction * max_connections well above "
                f"{relations * 3} is safe) and re-run, or run "
                "'manage.py maintain_partitions' first to clear the backlog."
            )
        cursor.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN IF EXISTS start_check')

        # Phase 3 -- build the replacement indexes over what remains.
        for index_name, suffix, columns, predicate in INDEX_SPECS:
            counts = {"built": 0, "skipped": 0}
            _build_index(
                cursor, TABLE, "p", index_name, columns, predicate, suffix, counts
            )
            logger.info(
                "%s: %s leaves built, %s already present",
                index_name,
                counts["built"],
                counts["skipped"],
            )
            if _index_valid(cursor, index_name) is not True:
                raise RuntimeError(
                    f"{index_name} is not valid -- some partitions were not "
                    "attached. Re-run this migration; it resumes where it stopped."
                )


def backwards(apps, schema_editor):
    """Restore the column, empty. The original values are not recoverable."""
    with schema_editor.connection.cursor() as cursor:
        for index_name, _, _, _ in INDEX_SPECS:
            cursor.execute(f'DROP INDEX IF EXISTS "{index_name}"')
        cursor.execute(
            f'ALTER TABLE "{TABLE}" ADD COLUMN IF NOT EXISTS start_check '
            "TIMESTAMPTZ NOT NULL DEFAULT now()"
        )


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run in a transaction, and each partition
    # drop must commit on its own so locks do not accumulate.
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
                        default=apps_models._generate_uuid7,
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
