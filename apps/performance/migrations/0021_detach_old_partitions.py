# Preparatory migration: detach and drop all child partitions from old
# performance tables so the next migration can DROP them without exceeding
# max_locks_per_transaction.
#
# After this migration the parent tables are empty shells — the Django app
# continues to work normally (transaction queries return empty results).
#
# Non-atomic so each DETACH / DROP autocommits individually.

from django.db import migrations


def _detach_and_drop_children(cursor, table_name):
    """Recursively detach and drop every child partition of *table_name*.

    Works for arbitrary nesting depth (RANGE → HASH, plain HASH, etc.).
    Each statement autocommits when the migration is non-atomic, so we
    never hold more than a handful of locks at once.
    """
    cursor.execute(
        """
        SELECT c.relname FROM pg_inherits
        JOIN pg_class c ON c.oid = inhrelid
        JOIN pg_class p ON p.oid = inhparent
        WHERE p.relname = %s
        ORDER BY c.relname
        """,
        [table_name],
    )
    children = [row[0] for row in cursor.fetchall()]

    for child in children:
        cursor.execute(
            f"ALTER TABLE {table_name} DETACH PARTITION {child};"
        )
        # Recurse into sub-partitions (e.g. hash children of a range partition)
        _detach_and_drop_children(cursor, child)
        cursor.execute(f"DROP TABLE IF EXISTS {child};")


def detach_old_performance_partitions(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for table in (
            "performance_transactionevent",
            "performance_transactiongroupaggregate",
            "performance_transactiongroup",
        ):
            _detach_and_drop_children(cursor, table)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("performance", "0020_alter_transactionevent_duration_and_more"),
    ]

    operations = [
        migrations.RunPython(
            code=detach_old_performance_partitions,
            reverse_code=noop,
        ),
    ]
