from django.db import migrations


class Migration(migrations.Migration):
    """
    Add (organization_id, date) index to UptimeCheckHourlyStatistic.

    Supports index scans for organization_id + date range queries in
    subscription_events_count_daily.
    """

    dependencies = [
        ("uptime", "0015_uptimecheckhourlystatistic"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE INDEX IF NOT EXISTS
                uptime_uptimecheckhourlystatistic_org_date
            ON uptime_uptimecheckhourlystatistic (organization_id, date);
            """,
            reverse_sql="""
            DROP INDEX IF EXISTS uptime_uptimecheckhourlystatistic_org_date;
            """,
        ),
    ]
