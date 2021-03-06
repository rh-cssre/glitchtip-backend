# Generated by Django 4.0.3 on 2022-04-13 15:22

import django.contrib.postgres.fields.hstore
import django.contrib.postgres.search
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('performance', '0006_rename_title_transactiongroup_transaction_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='transactionevent',
            name='tags',
            field=django.contrib.postgres.fields.hstore.HStoreField(default=dict),
        ),
        migrations.AddField(
            model_name='transactiongroup',
            name='search_vector',
            field=django.contrib.postgres.search.SearchVectorField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name='transactiongroup',
            name='tags',
            field=models.JSONField(default=dict),
        ),
    ]
