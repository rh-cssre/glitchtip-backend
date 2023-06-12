# Generated by Django 3.1.7 on 2021-02-19 19:31

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("issues", "0001_squashed_0010_auto_20210117_1543"),
    ]

    operations = [
        migrations.AlterField(
            model_name="issue",
            name="level",
            field=models.PositiveSmallIntegerField(
                choices=[
                    (0, "sample"),
                    (1, "debug"),
                    (2, "info"),
                    (3, "warning"),
                    (4, "error"),
                    (5, "fatal"),
                ],
                default=4,
            ),
        ),
    ]
