from django.db import migrations


class Migration(migrations.Migration):
    """
    Drop DEFAULT partitions created by 0019 for instances that already ran it.

    DEFAULT partitions conflict with nested RANGE->HASH partitioning used by
    the partition manager. They silently accumulate rows that should go into
    proper time-based partitions, and block future partition creation.
    """

    dependencies = [
        ("projects", "0020_add_log_statistics_and_quota"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            DROP TABLE IF EXISTS projects_issueeventprojecthourlystatistic_default;
            DROP TABLE IF EXISTS projects_transactioneventprojecthourlystatistic_default;
            """,
            reverse_sql="",
        ),
    ]
