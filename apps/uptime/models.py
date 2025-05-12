import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, URLValidator
from django.db import models
from django.db.models import OuterRef, Subquery
from django.urls import reverse
from django.utils.timezone import now
from django_extensions.db.fields import AutoSlugField
from psql_partition.models import PostgresPartitionedModel
from psql_partition.types import PostgresPartitioningMethod

from glitchtip.base_models import CreatedModel

from .constants import HTTP_MONITOR_TYPES, MonitorCheckReason, MonitorType


class MonitorManager(models.Manager):
    def with_check_annotations(self):
        """
        Adds MonitorCheck annotations:
        latest_is_up - Most recent check is_up result
        last_change - Most recent check where is_up state changed
        Example: Monitor state: { latest_is_up } since { last_change }
        """
        return self.annotate(
            latest_is_up=Subquery(
                MonitorCheck.objects.filter(
                    monitor_id=OuterRef("id"),
                )
                .order_by("-start_check")
                .values("is_up")[:1]
            ),
            last_change=Subquery(
                MonitorCheck.objects.filter(monitor_id=OuterRef("id"), is_change=True)
                .order_by("-start_check")
                .values("start_check")[:1]
            ),
        )


class OptionalSchemeURLValidator(URLValidator):
    def __call__(self, value):
        if "://" in value:
            super().__call__(value)


class Monitor(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    monitor_type = models.CharField(
        max_length=12, choices=MonitorType.choices, default=MonitorType.PING
    )
    endpoint_id = models.UUIDField(
        blank=True,
        null=True,
        editable=False,
        help_text="Used for referencing heartbeat endpoint",
    )
    name = models.CharField(max_length=200)
    url = models.CharField(
        max_length=2000, blank=True, validators=[OptionalSchemeURLValidator()]
    )
    expected_status = models.PositiveSmallIntegerField(
        default=200, blank=True, null=True
    )
    expected_body = models.CharField(max_length=2000, blank=True)
    environment = models.ForeignKey(
        "environments.Environment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    interval = models.PositiveSmallIntegerField(
        default=60,
        validators=[MaxValueValidator(86400), MinValueValidator(1)],
    )
    timeout = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MaxValueValidator(60), MinValueValidator(1)],
        help_text="Blank implies default value of 20",
    )

    objects = MonitorManager()

    class Meta:
        indexes = [models.Index(fields=["-created"])]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.monitor_type == MonitorType.HEARTBEAT and not self.endpoint_id:
            self.endpoint_id = uuid.uuid4()
        super().save(*args, **kwargs)
        # pylint: disable=import-outside-toplevel
        from apps.uptime.tasks import perform_checks

        if self.monitor_type != MonitorType.HEARTBEAT:
            perform_checks.apply_async(args=([self.pk],), countdown=1)

    def clean(self):
        if self.monitor_type in HTTP_MONITOR_TYPES:
            URLValidator()(self.url)
        if self.monitor_type != MonitorType.HEARTBEAT and not self.url:
            raise ValidationError("Monitor URL is required")

    def get_detail_url(self):
        return f"{settings.GLITCHTIP_URL.geturl()}/{self.project.organization.slug}/uptime-monitors/{self.pk}"

    @property
    def int_timeout(self):
        """Get timeout as integer (coalesce null as 20)"""
        return self.timeout or 20


class MonitorCheck(PostgresPartitionedModel, models.Model):
    monitor = models.ForeignKey(
        Monitor, on_delete=models.CASCADE, related_name="checks"
    )
    is_up = models.BooleanField()
    is_change = models.BooleanField(
        help_text="Indicates change to is_up status for associated monitor",
    )
    start_check = models.DateTimeField(
        default=now,
        help_text="Time when the start of this check was performed",
    )
    reason = models.PositiveSmallIntegerField(
        choices=MonitorCheckReason.choices, default=0, null=True, blank=True
    )
    response_time = models.PositiveIntegerField(
        blank=True, null=True, help_text="Reponse time in milliseconds"
    )
    data = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["monitor", "-start_check"]),
            models.Index(fields=["monitor", "is_change", "-start_check"]),
        ]
        ordering = ("-start_check",)

    class PartitioningMeta:
        method = PostgresPartitioningMethod.RANGE
        key = ["start_check"]

    def __str__(self):
        return self.up_or_down

    @property
    def up_or_down(self):
        if self.is_up:
            return "Up"
        return "Down"


class StatusPage(CreatedModel):
    """
    A status page is a collection of monitors that are available to view
    """

    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=200)
    slug = AutoSlugField(populate_from=["name"], max_length=200)
    is_public = models.BooleanField(
        help_text="When true, the status page URL is publicly accessible"
    )
    monitors = models.ManyToManyField(Monitor, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"], name="unique_organization_slug"
            )
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("status-page-detail", args=[self.organization.slug, self.slug])
