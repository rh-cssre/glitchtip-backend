# Manual migration to add indexes to partitioned MonitorCheck table
# Django doesn't detect these as missing because they're in model state from before
# the table was converted to partitioned in migration 0011.

from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Add indexes to uptime_monitorcheck partitioned table.

    These indexes are defined in the model but weren't created when the table
    was converted to a partitioned table in migration 0011.

    For partitioned tables, creating an index on the parent automatically
    creates corresponding indexes on all existing and future partitions.
    """

    dependencies = [
        ("uptime", "0011_storage_v2_uptime"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="monitorcheck",
            index=models.Index(
                fields=["monitor", "-start_check"],
                name="uptime_moni_monitor_a89b32_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="monitorcheck",
            index=models.Index(
                fields=["monitor", "is_change", "-start_check"],
                name="uptime_moni_monitor_b6d442_idx",
            ),
        ),
    ]
