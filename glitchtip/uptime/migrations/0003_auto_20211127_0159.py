# Generated by Django 3.2.9 on 2021-11-27 01:59

import datetime

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('uptime', '0002_auto_20210822_1617'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='monitor',
            name='ip_address',
        ),
        migrations.AlterField(
            model_name='monitor',
            name='interval',
            field=models.DurationField(default=datetime.timedelta(seconds=60), validators=[django.core.validators.MaxValueValidator(datetime.timedelta(seconds=86399))]),
        ),
        migrations.AlterField(
            model_name='monitor',
            name='monitor_type',
            field=models.CharField(choices=[('Ping', 'Ping'), ('GET', 'Get'), ('POST', 'Post'), ('SSL', 'Ssl'), ('Heartbeat', 'Heartbeat')], default='Ping', max_length=12),
        ),
        migrations.AlterField(
            model_name='monitor',
            name='url',
            field=models.URLField(blank=True, max_length=2000),
        ),
    ]
