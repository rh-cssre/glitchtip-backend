import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0016_add_slack_recipient_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectalert",
            name="environments",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=list,
                help_text=(
                    "Only alert on events and uptime monitors from these "
                    "environments. Empty matches every environment."
                ),
                size=100,
            ),
        ),
    ]
