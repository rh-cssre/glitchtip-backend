# Generated by Django 3.1 on 2020-08-21 14:46

import api_tokens.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api_tokens', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='apitoken',
            name='token',
            field=models.CharField(default=api_tokens.models.generate_token, editable=False, max_length=40, unique=True),
        ),
    ]
