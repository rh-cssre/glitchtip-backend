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
                help_text="Uncheck to reject every incoming event for this organization with a 429. Set by hand only: quota enforcement uses the event throttle rate below, so this stays off until someone turns it back on.",
            ),
        ),
        migrations.AlterField(
            model_name="organization",
            name="event_throttle_rate",
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text="Percentage of this organization's incoming events to drop. Set automatically by the quota check, which overwrites anything entered here; 100 rejects everything, as unchecking the box above does.",
                validators=[django.core.validators.MaxValueValidator(100)],
            ),
        ),
    ]
