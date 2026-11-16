from django.db import models


class PostgresPartitionedModel(models.Model):
    class Meta:
        abstract = True
