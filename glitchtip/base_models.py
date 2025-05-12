from django.db import models
from psql_partition.models import PostgresPartitionedModel
from psql_partition.types import PostgresPartitioningMethod


class CreatedModel(models.Model):
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        abstract = True


class SoftDeleteManager(models.Manager):
    def __init__(self, is_deleted=False):
        super().__init__()
        self.is_deleted = is_deleted

    def get_queryset(self):
        return models.QuerySet(self.model, using=self._db).filter(
            is_deleted=self.is_deleted
        )


class SoftDeleteModel(models.Model):
    """Based on https://tomisin.dev/blog/implementing-soft-delete-in-django-an-intuitive-guide"""

    is_deleted = models.BooleanField(default=False)
    objects = models.Manager()
    undeleted_objects = SoftDeleteManager()
    marked_for_deletion = SoftDeleteManager(is_deleted=True)

    class Meta:
        abstract = True

    def delete(self, *args, **kwargs):
        """Mark the record as deleted instead of deleting it"""
        self.is_deleted = True
        self.save(update_fields=["is_deleted"])

    def force_delete(self, *args, **kwargs):
        """Delete the record from the database"""
        super().delete(*args, **kwargs)


class AggregationModel(PostgresPartitionedModel, models.Model):
    """
    Partitioned base model for storing aggregate statistics in such as per
    time delta counts of events
    """

    date = models.DateTimeField()
    count = models.PositiveIntegerField()

    class Meta:
        abstract = True

    class PartitioningMeta:
        method = PostgresPartitioningMethod.RANGE
        key = ["date"]
