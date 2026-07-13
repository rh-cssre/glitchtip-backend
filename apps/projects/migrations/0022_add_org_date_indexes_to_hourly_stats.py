from django.db import migrations


class Migration(migrations.Migration):
    """
    Add (organization_id, date) indexes to hourly statistic tables.

    PKs lead with project_id, so filtering by organization_id + date causes
    sequential partition scans. These indexes allow direct row seeks.
    """

    dependencies = [
        ("projects", "0021_drop_default_partitions"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE INDEX IF NOT EXISTS
                projects_issueeventprojecthourlystatistic_org_date
            ON projects_issueeventprojecthourlystatistic (organization_id, date);
            """,
            reverse_sql="""
            DROP INDEX IF EXISTS projects_issueeventprojecthourlystatistic_org_date;
            """,
        ),
        migrations.RunSQL(
            sql="""
            CREATE INDEX IF NOT EXISTS
                projects_transactioneventprojecthourlystatistic_org_date
            ON projects_transactioneventprojecthourlystatistic (organization_id, date);
            """,
            reverse_sql="""
            DROP INDEX IF EXISTS projects_transactioneventprojecthourlystatistic_org_date;
            """,
        ),
        migrations.RunSQL(
            sql="""
            CREATE INDEX IF NOT EXISTS
                projects_logprojecthourlystatistic_org_date
            ON projects_logprojecthourlystatistic (organization_id, date);
            """,
            reverse_sql="""
            DROP INDEX IF EXISTS projects_logprojecthourlystatistic_org_date;
            """,
        ),
    ]
