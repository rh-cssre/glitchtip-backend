from django.core.management.base import BaseCommand
from django.db import connection

RESYNC_SQL = """
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


class Command(BaseCommand):
    help = "Re-sync cached_is_up and cached_last_change on Monitor from MonitorCheck data"

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute(RESYNC_SQL)
            self.stdout.write(
                self.style.SUCCESS(f"Updated {cursor.rowcount} monitors")
            )
