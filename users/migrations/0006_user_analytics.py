# Generated by Django 3.2.8 on 2021-10-14 18:16

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0005_auto_20200612_2011"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="analytics",
            field=models.JSONField(null=True, blank=True),
        ),
    ]
