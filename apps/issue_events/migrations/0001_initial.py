# Generated by Django 5.0.2 on 2024-02-14 14:07

import django.contrib.postgres.indexes
import django.contrib.postgres.search
import django.db.models.deletion
import django.utils.timezone
import psqlextra.backend.migrations.operations.add_default_partition
import psqlextra.backend.migrations.operations.create_partitioned_model
import psqlextra.manager.manager
import psqlextra.models.partitioned
import psqlextra.types
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('projects', '0013_merge_20231017_1350'),
        ('releases', '0001_squashed_0004_alter_release_id_alter_releasefile_id_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Comment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('text', models.TextField(blank=True, null=True)),
                ('user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('-created',),
            },
        ),
        migrations.CreateModel(
            name='Issue',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_deleted', models.BooleanField(default=False)),
                ('culprit', models.CharField(blank=True, max_length=1024, null=True)),
                ('is_public', models.BooleanField(default=False)),
                ('level', models.PositiveSmallIntegerField(choices=[(0, 'sample'), (1, 'debug'), (2, 'info'), (3, 'warning'), (4, 'error'), (5, 'fatal')], default=4)),
                ('metadata', models.JSONField()),
                ('title', models.CharField(max_length=255)),
                ('type', models.PositiveSmallIntegerField(choices=[(0, 'default'), (1, 'error'), (2, 'csp')], default=0)),
                ('status', models.PositiveSmallIntegerField(choices=[(0, 'unresolved'), (1, 'resolved'), (2, 'ignored')], default=0)),
                ('short_id', models.PositiveIntegerField(null=True)),
                ('search_vector', django.contrib.postgres.search.SearchVectorField(default='', editable=False)),
                ('count', models.PositiveIntegerField(default=1, editable=False)),
                ('first_seen', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('last_seen', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='issues', to='projects.project')),
            ],
            options={
                'base_manager_name': 'objects',
            },
        ),
        psqlextra.backend.migrations.operations.create_partitioned_model.PostgresCreatePartitionedModel(
            name='IssueEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('type', models.PositiveSmallIntegerField(choices=[(0, 'default'), (1, 'error'), (2, 'csp')], default=0)),
                ('timestamp', models.DateTimeField(help_text='Time at which event happened')),
                ('received', models.DateTimeField(help_text='Time at which GlitchTip accepted event')),
                ('title', models.CharField(max_length=255)),
                ('transaction', models.CharField(max_length=200)),
                ('level', models.PositiveSmallIntegerField(choices=[(0, 'sample'), (1, 'debug'), (2, 'info'), (3, 'warning'), (4, 'error'), (5, 'fatal')], default=4)),
                ('data', models.JSONField()),
                ('tags', models.JSONField()),
                ('issue', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='issue_events.issue')),
                ('release', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='releases.release')),
            ],
            options={
                'abstract': False,
                'base_manager_name': 'objects',
            },
            partitioning_options={
                'method': psqlextra.types.PostgresPartitioningMethod['RANGE'],
                'key': ['received'],
            },
            bases=(psqlextra.models.partitioned.PostgresPartitionedModel,),
            managers=[
                ('objects', psqlextra.manager.manager.PostgresManager()),
            ],
        ),
        psqlextra.backend.migrations.operations.add_default_partition.PostgresAddDefaultPartition(
            model_name='IssueEvent',
            name='default',
        ),
        migrations.CreateModel(
            name='IssueHash',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('value', models.UUIDField(db_index=True)),
                ('issue', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='hashes', to='issue_events.issue')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='projects.project')),
            ],
        ),
        psqlextra.backend.migrations.operations.create_partitioned_model.PostgresCreatePartitionedModel(
            name='IssueTag',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateTimeField()),
                ('count', models.PositiveIntegerField(default=1)),
                ('issue', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='issue_events.issue')),
            ],
            partitioning_options={
                'method': psqlextra.types.PostgresPartitioningMethod['RANGE'],
                'key': ['date'],
            },
            bases=(psqlextra.models.partitioned.PostgresPartitionedModel,),
            managers=[
                ('objects', psqlextra.manager.manager.PostgresManager()),
            ],
        ),
        psqlextra.backend.migrations.operations.add_default_partition.PostgresAddDefaultPartition(
            model_name='IssueTag',
            name='default',
        ),
        migrations.CreateModel(
            name='TagKey',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('key', models.CharField(max_length=255, unique=True)),
            ],
        ),
        migrations.CreateModel(
            name='TagValue',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('value', models.CharField(max_length=255, unique=True)),
            ],
        ),
        migrations.AddField(
            model_name='comment',
            name='issue',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='comments', to='issue_events.issue'),
        ),
        migrations.AddField(
            model_name='issuetag',
            name='tag_key',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='issue_events.tagkey'),
        ),
        migrations.AddField(
            model_name='issuetag',
            name='tag_value',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='issue_events.tagvalue'),
        ),
        migrations.CreateModel(
            name='UserReport',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('event_id', models.UUIDField()),
                ('name', models.CharField(max_length=128)),
                ('email', models.EmailField(max_length=254)),
                ('comments', models.TextField()),
                ('issue', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to='issue_events.issue')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='projects.project')),
            ],
        ),
        migrations.AddIndex(
            model_name='issue',
            index=django.contrib.postgres.indexes.GinIndex(fields=['search_vector'], name='issue_event_search__346c17_gin'),
        ),
        migrations.AddConstraint(
            model_name='issue',
            constraint=models.UniqueConstraint(fields=('project', 'short_id'), name='project_short_id_unique'),
        ),
        migrations.AddConstraint(
            model_name='issuehash',
            constraint=models.UniqueConstraint(fields=('project', 'value'), name='issue hash project'),
        ),
        migrations.AddConstraint(
            model_name='issuetag',
            constraint=models.UniqueConstraint(fields=('issue', 'date', 'tag_key', 'tag_value'), name='issue_tag_key_value_unique'),
        ),
        migrations.AddConstraint(
            model_name='userreport',
            constraint=models.UniqueConstraint(fields=('project', 'event_id'), name='project_event_unique'),
        ),
    ]
