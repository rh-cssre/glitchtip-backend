import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("organizations_ext", "0015_organization_overage_last_report"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organization",
            name="is_accepting_events",
            field=models.BooleanField(
                default=True,
                help_text="When false, every incoming event for this organization is rejected with a 429. Set by hand only: quota enforcement uses event_throttle_rate instead, so this stays off until someone turns it back on.",
            ),
        ),
        migrations.AlterField(
            model_name="organization",
            name="event_throttle_rate",
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text="Percentage of this organization's incoming events to drop. Set automatically by the quota check, which overwrites anything entered here; 100 rejects everything, as clearing is_accepting_events does.",
                validators=[django.core.validators.MaxValueValidator(100)],
            ),
        ),
    ]
