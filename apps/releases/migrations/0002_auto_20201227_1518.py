# Generated by Django 3.1.4 on 2020-12-27 15:18

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("releases", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="release",
            name="data",
            field=models.JSONField(default=dict),
        ),
    ]