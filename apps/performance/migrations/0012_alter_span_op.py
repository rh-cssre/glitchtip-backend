# Generated by Django 5.0.2 on 2024-02-29 18:19

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('performance', '0011_alter_transactionevent_duration'),
    ]

    operations = [
        migrations.AlterField(
            model_name='span',
            name='op',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
