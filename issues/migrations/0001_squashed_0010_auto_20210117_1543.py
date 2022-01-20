# Generated by Django 3.1.5 on 2021-01-17 17:02

import django.contrib.postgres.fields.jsonb
import django.contrib.postgres.indexes
import django.contrib.postgres.search
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import uuid


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("releases", "0002_auto_20201227_1518"),
        ("projects", "0001_initial"),
        ("projects", "0003_projectcounter"),
    ]

    operations = [
        migrations.CreateModel(
            name="EventTag",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("key", models.CharField(max_length=255)),
                ("value", models.CharField(max_length=225)),
            ],
        ),
        migrations.CreateModel(
            name="Issue",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("culprit", models.CharField(blank=True, max_length=1024, null=True)),
                ("has_seen", models.BooleanField(default=False)),
                ("is_public", models.BooleanField(default=False)),
                (
                    "level",
                    models.PositiveSmallIntegerField(
                        choices=[
                            (0, "sample"),
                            (1, "debug"),
                            (2, "info"),
                            (3, "warning"),
                            (4, "error"),
                            (5, "fatal"),
                        ],
                        default=0,
                    ),
                ),
                ("metadata", django.contrib.postgres.fields.jsonb.JSONField()),
                ("title", models.CharField(max_length=255)),
                (
                    "type",
                    models.PositiveSmallIntegerField(
                        choices=[(0, "default"), (1, "error"), (2, "csp")], default=0
                    ),
                ),
                (
                    "status",
                    models.PositiveSmallIntegerField(
                        choices=[(0, "unresolved"), (1, "resolved"), (2, "ignored")],
                        default=0,
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="projects.project",
                    ),
                ),
                ("count", models.PositiveIntegerField(default=1, editable=False)),
                (
                    "last_seen",
                    models.DateTimeField(
                        auto_now_add=True, default=django.utils.timezone.now
                    ),
                ),
                (
                    "search_vector",
                    django.contrib.postgres.search.SearchVectorField(
                        editable=False, null=True
                    ),
                ),
            ],
            options={"unique_together": {("title", "culprit", "project", "type")},},
        ),
        migrations.CreateModel(
            name="Event",
            fields=[
                (
                    "event_id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "timestamp",
                    models.DateTimeField(
                        blank=True,
                        help_text="Date created as claimed by client it came from",
                        null=True,
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("data", django.contrib.postgres.fields.jsonb.JSONField()),
                (
                    "issue",
                    models.ForeignKey(
                        help_text="Sentry calls this a group",
                        on_delete=django.db.models.deletion.CASCADE,
                        to="issues.issue",
                    ),
                ),
            ],
            options={"ordering": ["-created"],},
        ),
        migrations.AddIndex(
            model_name="issue",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["search_vector"], name="search_vector_idx"
            ),
        ),
        migrations.AddField(
            model_name="issue",
            name="short_id",
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AlterUniqueTogether(
            name="issue",
            unique_together={
                ("title", "culprit", "project", "type"),
                ("project", "short_id"),
            },
        ),
        migrations.RunSQL(
            sql="\nDROP TRIGGER IF EXISTS increment_project_counter on issues_issue;\n\nCREATE OR REPLACE FUNCTION increment_project_counter() RETURNS trigger AS $$\nDECLARE\n    counter_value int;\nBEGIN\n    INSERT INTO projects_projectcounter (value, project_id)\n    VALUES (0, NEW.project_id)\n    ON CONFLICT (project_id) DO UPDATE SET value = projects_projectcounter.value + 1\n    RETURNING value into counter_value;\n    NEW.short_id=counter_value;\n    RETURN NEW;\nEND;\n$$ LANGUAGE plpgsql;;\n\nCREATE TRIGGER increment_project_counter BEFORE INSERT\nON issues_issue FOR EACH ROW EXECUTE PROCEDURE\nincrement_project_counter();\n",
            reverse_sql="DROP TRIGGER IF EXISTS increment_project_counter on issues_issue; DROP FUNCTION IF EXISTS increment_project_counter;",
        ),
        migrations.CreateModel(
            name="EventTagKey",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("key", models.CharField(max_length=255, unique=True)),
            ],
        ),
        migrations.AddField(
            model_name="event",
            name="tags",
            field=models.ManyToManyField(blank=True, to="issues.EventTag"),
        ),
        migrations.AlterField(
            model_name="eventtag",
            name="key",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="issues.eventtagkey"
            ),
        ),
        migrations.AlterUniqueTogether(
            name="eventtag", unique_together={("key", "value")},
        ),
        migrations.AlterField(
            model_name="event", name="data", field=models.JSONField(),
        ),
        migrations.AlterField(
            model_name="issue", name="metadata", field=models.JSONField(),
        ),
        migrations.AlterField(
            model_name="issue",
            name="last_seen",
            field=models.DateTimeField(auto_now_add=True, db_index=True),
        ),
        migrations.AddField(
            model_name="event",
            name="release",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="releases.release",
            ),
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.AlterModelTable(name="Event", table="events_event",),
                migrations.AlterModelTable(
                    name="EventTagKey", table="events_eventtagkey",
                ),
                migrations.AlterModelTable(name="EventTag", table="events_eventtag",),
            ],
            state_operations=[
                migrations.DeleteModel(name="Event",),
                migrations.DeleteModel(name="EventTagKey",),
                migrations.DeleteModel(name="EventTag",),
            ],
        ),
        migrations.AlterField(
            model_name="issue",
            name="type",
            field=models.PositiveSmallIntegerField(
                choices=[(0, "default"), (1, "error"), (2, "csp"), (3, "transaction")],
                default=0,
            ),
        ),
    ]
