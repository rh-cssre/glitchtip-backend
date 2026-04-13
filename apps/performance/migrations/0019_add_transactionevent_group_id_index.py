# Add index on group_id for performance_transactionevent.
#
# Without this index, CASCADE FK checks and maintenance queries must
# sequentially scan every sub-partition. With ~90 daily × N hash partitions,
# this caused statement timeouts in production.
#
# Not using CONCURRENTLY because Postgres does not support it on partitioned
# tables — the index propagates to all existing sub-partitions automatically.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("performance", "0018_storage_v2_performance"),
    ]

    operations = [
        migrations.RunSQL(
            sql="CREATE INDEX IF NOT EXISTS transactionevent_group_id_idx ON performance_transactionevent (group_id);",
            reverse_sql="DROP INDEX IF EXISTS transactionevent_group_id_idx;",
        ),
    ]
