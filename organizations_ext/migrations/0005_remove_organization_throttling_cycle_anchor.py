# Generated by Django 3.0.6 on 2020-05-23 14:48

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('organizations_ext', '0004_organization_throttling_cycle_anchor'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='organization',
            name='throttling_cycle_anchor',
        ),
    ]
