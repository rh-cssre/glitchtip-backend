# Generated by Django 3.2 on 2021-04-11 19:13

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("issues", "0003_alter_issue_id"),
    ]

    operations = [
        migrations.AlterField(
            model_name="issue",
            name="id",
            field=models.AutoField(
                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
    ]
