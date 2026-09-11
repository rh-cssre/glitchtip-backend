from django.core.exceptions import ImproperlyConfigured
from django.db import models

ORG_SCOPED_MODELS: list[type] = []


class OrgScopedModel:
    """Declares how the folded gate reaches this model's organization id.

    ``org_field`` is an ORM path ending in the organization id column. Prefer
    the local ``"organization_id"``; every ``__`` hop is a join on every gated
    query, and reaching ``organization`` itself is much slower.

    Not a ``models.Model``: it carries no fields and adds no table, so mixing
    it in needs no migration.
    """

    org_field: str

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        field = getattr(cls, "org_field", None)
        if not isinstance(field, str) or not field:
            raise ImproperlyConfigured(
                f"{cls.__qualname__} is an OrgScopedModel but declares no "
                "org_field -- the gate has no column to filter on"
            )
        if field == "organization" or field.endswith("__organization"):
            raise ImproperlyConfigured(
                f"{cls.__qualname__}.org_field is {field!r}, which joins "
                f"organization from the data model. Use {field}_id."
            )
        ORG_SCOPED_MODELS.append(cls)


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


class AggregationModel(models.Model):
    """
    Base model for storing aggregate statistics in such as per
    time delta counts of events
    """

    date = models.DateTimeField()
    count = models.PositiveIntegerField()

    class Meta:
        abstract = True
