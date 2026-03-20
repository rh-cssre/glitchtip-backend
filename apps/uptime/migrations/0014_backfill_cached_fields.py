from django.db import migrations

BACKFILL_SQL = """
UPDATE uptime_monitor m
SET
    cached_is_up = sub.is_up,
    cached_last_change = sub.last_change
FROM (
    SELECT
        mon.id AS monitor_id,
        (
            SELECT mc.is_up
            FROM uptime_monitorcheck mc
            WHERE mc.monitor_id = mon.id
              AND mc.organization_id = mon.organization_id
            ORDER BY mc.start_check DESC
            LIMIT 1
        ) AS is_up,
        (
            SELECT mc.start_check
            FROM uptime_monitorcheck mc
            WHERE mc.monitor_id = mon.id
              AND mc.organization_id = mon.organization_id
              AND mc.is_change = TRUE
            ORDER BY mc.start_check DESC
            LIMIT 1
        ) AS last_change
    FROM uptime_monitor mon
) sub
WHERE m.id = sub.monitor_id;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("uptime", "0013_monitor_cached_fields"),
    ]

    operations = [
        migrations.RunSQL(
            sql=BACKFILL_SQL,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
