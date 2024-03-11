# Generated by Django 4.1.3 on 2022-12-10 15:29

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        (
            "organizations_ext",
            "0001_squashed_0003_alter_organization_id_alter_organization_users_and_more",
        ),
    ]

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
                ("checksum", models.CharField(max_length=40, unique=True)),
                ("size", models.PositiveIntegerField(null=True)),
                ("blob", models.FileField(default="", upload_to="uploads/file_blobs")),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="File",
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
                ("name", models.TextField()),
                ("headers", models.JSONField(blank=True, null=True)),
                ("size", models.PositiveIntegerField(null=True)),
                ("checksum", models.CharField(db_index=True, max_length=40, null=True)),
                ("blobs", models.ManyToManyField(to="files.fileblob")),
                ("type", models.CharField(default="", max_length=64)),
                (
                    "blob",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="files.fileblob",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="FileBlobIndex",
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
                ("offset", models.PositiveIntegerField()),
                (
                    "blob",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="files.fileblob"
                    ),
                ),
                (
                    "file",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="files.file"
                    ),
                ),
            ],
            options={
                "unique_together": {("file", "blob", "offset")},
            },
        ),
        migrations.RemoveField(
            model_name="file",
            name="blobs",
        ),
    ]
