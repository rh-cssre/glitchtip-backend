# Generated by Django 3.2 on 2021-04-11 19:13

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="FileBlob",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("upload", models.FileField(upload_to="uploads/")),
                ("checksum", models.CharField(max_length=40, unique=True)),
            ],
            options={
                "abstract": False,
            },
        ),
    ]
