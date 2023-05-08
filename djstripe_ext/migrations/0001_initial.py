# Generated by Django 3.1.7 on 2021-03-12 19:09

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("djstripe", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SubscriptionQuotaWarning",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("notice_last_sent", models.DateTimeField(auto_now=True)),
                (
                    "subscription",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="djstripe.subscription",
                    ),
                ),
            ],
        ),
    ]
