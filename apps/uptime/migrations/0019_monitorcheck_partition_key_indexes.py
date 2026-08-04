"""Re-key MonitorCheck's secondary indexes from ``start_check`` to ``id``.

``uptime_monitorcheck`` is RANGE partitioned on ``id`` (a UUIDv7) and HASH
sub-partitioned on ``organization_id``. Every read of it is "the most recent N
checks for this monitor", but the indexes sorted by ``start_check`` -- a column
that has nothing to do with the partition key. Postgres therefore could not
plan an ordered Append; it had to Merge Append, opening an index scan on every
partition just to discover which one held the newest row. Indexing by the
partition key instead lets it walk partitions newest-first and stop as soon as
LIMIT is satisfied.

Two indexes are created:

* ``uptime_check_monitor_id_idx`` -- ``(monitor_id, id DESC)``, replacing
  ``(monitor_id, start_check DESC)``. Roughly the same size as the index it
  replaces.
* ``uptime_check_change_id_idx`` -- ``(monitor_id, id DESC) WHERE is_change``,
  replacing the full ``(monitor_id, is_change, start_check DESC)``. ``is_change``
  marks an up/down transition and is 0.03-0.6% of rows, so the partial form is
  orders of magnitude smaller than the three-column index it replaces and
  builds in seconds.

The old indexes are dropped in the next migration, so a deploy that stops here
is still fully functional (just carrying redundant indexes).

Why this migration is hand-written rather than ``AddIndex``
-----------------------------------------------------------
``CREATE INDEX`` on a partitioned parent takes ACCESS EXCLUSIVE on the parent
*and every leaf* for the whole build -- all reads and all check ingest block
until it finishes. Postgres does not accept CONCURRENTLY on a partitioned
parent, but it does accept it per leaf, and a leaf index can be adopted into
the partitioned index afterwards. So this builds bottom-up:

    CREATE INDEX ... ON ONLY <parent>            -- metadata only, invalid
    CREATE INDEX CONCURRENTLY ... ON <leaf>      -- online, one leaf at a time
    ALTER INDEX <parent_idx> ATTACH PARTITION <leaf_idx>

The parent index flips to valid automatically once the last leaf is attached.
Nothing here holds a lock longer than a single leaf's build, so ingest and the
monitors page keep working throughout.

Cost scales with leaf count, not total row count, because each leaf is a
separate small build. A new install has a handful of partitions and finishes in
well under a second. Measured at a leaf size typical of a large install
(~78k rows / ~7 MB), one CONCURRENTLY build takes ~70-120 ms, so ~1,500 leaves
x two indexes lands in the single-digit minutes.

The figure to watch is not the data volume but how long the database's
transactions run: CONCURRENTLY waits twice per leaf for transactions older than
its snapshot, so one long-lived transaction slows every remaining leaf. Uptime
ingest is short-transaction, so this is normally negligible; an install running
long analytics transactions against the same database should expect
proportionally longer.

Overrunning is not harmful. Nothing blocks while it runs, and it is resumable --
re-running skips leaves that already have a valid index, and any index left
INVALID by an interrupted build is dropped and rebuilt -- so a deploy killed
part-way simply continues where it stopped.
"""

import logging

from django.db import connection, migrations, models

logger = logging.getLogger(__name__)

TABLE = "uptime_monitorcheck"

# (parent index name, child name suffix, indexed columns, predicate)
INDEX_SPECS = (
    ("uptime_check_monitor_id_idx", "mid", "(monitor_id, id DESC)", ""),
    (
        "uptime_check_change_id_idx",
        "cid",
        "(monitor_id, id DESC)",
        " WHERE is_change",
    ),
)


def _children(cursor, relname):
    """Immediate partitions of ``relname`` as (name, relkind) pairs."""
    cursor.execute(
        """
        SELECT c.relname, c.relkind
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = %s
          AND p.relnamespace = 'public'::regnamespace
        ORDER BY c.relname
        """,
        [relname],
    )
    return cursor.fetchall()


def _index_state(cursor, index_name):
    """Return None if absent, else True/False for indisvalid."""
    cursor.execute(
        """
        SELECT i.indisvalid
        FROM pg_class c
        JOIN pg_index i ON i.indexrelid = c.oid
        WHERE c.relname = %s AND c.relnamespace = 'public'::regnamespace
        """,
        [index_name],
    )
    row = cursor.fetchone()
    return None if row is None else row[0]


def _is_attached(cursor, index_name):
    cursor.execute(
        """
        SELECT 1
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE c.relname = %s AND c.relnamespace = 'public'::regnamespace
        """,
        [index_name],
    )
    return cursor.fetchone() is not None


def _child_index_name(child_relname, suffix):
    # Postgres caps identifiers at 63 bytes; partition names are already long.
    return f"{child_relname[: 63 - len(suffix) - 1]}_{suffix}"


def _build(cursor, relname, relkind, index_name, columns, predicate, suffix, counts):
    """Create ``index_name`` on ``relname``, recursing into its partitions."""
    if relkind == "p":
        if _index_state(cursor, index_name) is None:
            # ON ONLY: metadata only, no scan, no lock on the leaves. The index
            # stays invalid (and unused by the planner) until every leaf is
            # attached below.
            cursor.execute(
                f'CREATE INDEX "{index_name}" ON ONLY "{relname}" {columns}{predicate}'
            )
        for child, child_kind in _children(cursor, relname):
            child_index = _child_index_name(child, suffix)
            _build(
                cursor,
                child,
                child_kind,
                child_index,
                columns,
                predicate,
                suffix,
                counts,
            )
            if not _is_attached(cursor, child_index):
                cursor.execute(
                    f'ALTER INDEX "{index_name}" ATTACH PARTITION "{child_index}"'
                )
        return

    valid = _index_state(cursor, index_name)
    if valid is False:
        # Left behind by an interrupted CONCURRENTLY build. It will never become
        # valid on its own and cannot be attached, so replace it.
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

    total = counts["built"] + counts["skipped"]
    if total % 200 == 0:
        logger.info(
            "monitorcheck index %s: %s leaves built, %s already present",
            suffix,
            counts["built"],
            counts["skipped"],
        )


def create_indexes(apps, schema_editor):
    if connection.in_atomic_block:
        raise RuntimeError(
            "0019 must run outside a transaction (atomic = False); "
            "CREATE INDEX CONCURRENTLY cannot run inside one."
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relkind FROM pg_class WHERE relname = %s "
            "AND relnamespace = 'public'::regnamespace",
            [TABLE],
        )
        row = cursor.fetchone()
        if row is None:
            logger.warning("%s does not exist; skipping index build", TABLE)
            return
        for index_name, suffix, columns, predicate in INDEX_SPECS:
            counts = {"built": 0, "skipped": 0}
            _build(
                cursor, TABLE, row[0], index_name, columns, predicate, suffix, counts
            )
            logger.info(
                "monitorcheck index %s complete: %s leaves built, %s already present",
                index_name,
                counts["built"],
                counts["skipped"],
            )


def drop_indexes(apps, schema_editor):
    with connection.cursor() as cursor:
        for index_name, _, _, _ in INDEX_SPECS:
            cursor.execute(f'DROP INDEX IF EXISTS "{index_name}"')


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    atomic = False

    dependencies = [
        ("uptime", "0018_monitor_cached_consecutive_down_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                # State-only: default ordering follows the partition key too, so
                # a bare MonitorCheck queryset gets the ordered-Append plan
                # rather than the all-partitions Merge Append.
                migrations.AlterModelOptions(
                    name="monitorcheck",
                    options={"ordering": ("-id",)},
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
            database_operations=[
                migrations.RunPython(create_indexes, drop_indexes),
            ],
        ),
    ]
