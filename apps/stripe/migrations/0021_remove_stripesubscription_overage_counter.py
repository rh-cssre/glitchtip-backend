from django.db import migrations


class Migration(migrations.Migration):
    """Drop the per-subscription delta counter.

    Not carried over: the meter now aggregates by ``last`` and reports are
    cycle-to-date totals, so the old "units already sent as increments" figure
    has no meaning in the new scheme. The organization's last-report record
    starts empty and fills on the first sweep.
    """

    dependencies = [
        ("stripe", "0020_stripeproduct_uptime_monitors"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="stripesubscription",
            name="overage_period_start",
        ),
        migrations.RemoveField(
            model_name="stripesubscription",
            name="overage_units_reported",
        ),
    ]
