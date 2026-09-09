from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("organizations_ext", "0014_organization_member_guard"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="overage_last_reported_units",
            field=models.PositiveBigIntegerField(
                default=0,
                help_text="Cycle-to-date billable overage last reported to the meter.",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="overage_last_reported_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Measurement time of the last overage report.",
                null=True,
            ),
        ),
    ]
