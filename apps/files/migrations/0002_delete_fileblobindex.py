from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("files", "0001_squashed_0009_alter_file_size"),
    ]

    operations = [
        migrations.DeleteModel(
            name="FileBlobIndex",
        ),
    ]
