# Generated by Django 3.0rc1 on 2019-11-30 20:39

import django.contrib.postgres.fields.jsonb
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Project',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('slug', models.SlugField()),
                ('name', models.CharField(max_length=200)),
                ('date_added', models.DateTimeField(auto_now_add=True)),
                ('platform', models.CharField(max_length=64, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='ProjectKey',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(blank=True, max_length=64)),
                ('public_key', models.CharField(max_length=32, unique=True)),
                ('date_added', models.DateTimeField(auto_now_add=True)),
                ('rate_limit_count', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('rate_limit_window', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('data', django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='projects.Project')),
            ],
        ),
    ]