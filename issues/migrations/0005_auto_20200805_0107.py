# Generated by Django 3.1 on 2020-08-05 01:07

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('issues', '0004_auto_20200804_0053'),
    ]

    operations = [
        migrations.AlterField(
            model_name='event',
            name='data',
            field=models.JSONField(),
        ),
        migrations.AlterField(
            model_name='issue',
            name='metadata',
            field=models.JSONField(),
        ),
    ]
