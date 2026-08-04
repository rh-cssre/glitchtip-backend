"""Drop the ``start_check``-ordered MonitorCheck indexes superseded by 0019.

Kept as its own migration so the build (0019, long but non-blocking) and the
drop (fast but briefly exclusive) can be applied at different times, and so a
rollback of the drop does not force a rebuild.

``DROP INDEX`` on a partitioned index cascades to every leaf in one
transaction, which means one lock per leaf. That is metadata work -- no scan,
no rewrite -- but on an install with thousands of leaves it wants
``max_locks_per_transaction`` headroom, so the two indexes are dropped in
separate statements rather than one.

On a large install this reclaims a lot of space: these two indexes measured
6.5 GB and 8.5 GB against a 10 GB heap, and 0019's replacements are ~6.5 GB and
a few tens of MB. It also removes two index insertions from every check write.
"""

from django.db import migrations

OLD_INDEXES = (
    "uptime_moni_monitor_a89b32_idx",
    "uptime_moni_monitor_b6d442_idx",
)


class Migration(migrations.Migration):
    dependencies = [
        ("uptime", "0019_monitorcheck_partition_key_indexes"),
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
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=f'DROP INDEX IF EXISTS "{OLD_INDEXES[0]}";',
                    reverse_sql=(
                        "CREATE INDEX IF NOT EXISTS "
                        f'"{OLD_INDEXES[0]}" ON uptime_monitorcheck '
                        "(monitor_id, start_check DESC);"
                    ),
                ),
                migrations.RunSQL(
                    sql=f'DROP INDEX IF EXISTS "{OLD_INDEXES[1]}";',
                    reverse_sql=(
                        "CREATE INDEX IF NOT EXISTS "
                        f'"{OLD_INDEXES[1]}" ON uptime_monitorcheck '
                        "(monitor_id, is_change, start_check DESC);"
                    ),
                ),
            ],
        ),
    ]
