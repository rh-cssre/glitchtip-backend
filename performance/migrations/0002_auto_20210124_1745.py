# Generated by Django 3.1.5 on 2021-01-24 17:45

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("performance", "0001_initial"),
    ]

    operations = [migrations.DeleteModel("TransactionEvent")]
