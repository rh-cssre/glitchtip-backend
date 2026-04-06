# Convert duration_histogram from JSONB to integer[] (50-element array).
# Pre-production: no data migration needed — drop and re-add the column.

import django.contrib.postgres.fields
from django.db import migrations, models

import apps.performance.histogram


class Migration(migrations.Migration):
    dependencies = [
        ("performance", "0022_performance_v3"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="transactiongroup",
                    name="duration_histogram",
                    field=django.contrib.postgres.fields.ArrayField(
                        base_field=models.IntegerField(),
                        default=apps.performance.histogram.new_histogram,
                        size=50,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    ALTER TABLE performance_transactiongroup
                        DROP COLUMN duration_histogram,
                        ADD COLUMN duration_histogram integer[] NOT NULL
                            DEFAULT (array_fill(0, ARRAY[50]));
                    """,
                    reverse_sql="""
                    ALTER TABLE performance_transactiongroup
                        DROP COLUMN duration_histogram,
                        ADD COLUMN duration_histogram jsonb NOT NULL DEFAULT '{}';
                    """,
                ),
            ],
        ),
    ]
