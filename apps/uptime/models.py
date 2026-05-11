import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, URLValidator
from django.db import models
from django.db.models import F
from django.urls import reverse
from django.utils.timezone import now
from django_extensions.db.fields import AutoSlugField

from glitchtip.base_models import CreatedModel
from glitchtip.partition_manager import UUID7Helper

from .constants import HTTP_MONITOR_TYPES, MonitorCheckReason, MonitorType


class MonitorManager(models.Manager):
    def with_check_annotations(self):
        """
        Adds MonitorCheck annotations:
        latest_is_up - Most recent check is_up result
        last_change - Most recent check where is_up state changed
        Example: Monitor state: { latest_is_up } since { last_change }

        These are now denormalized onto the Monitor model and read via F().
        """
        return self.annotate(
            latest_is_up=F("cached_is_up"),
            last_change=F("cached_last_change"),
        )


class OptionalSchemeURLValidator(URLValidator):
    def __call__(self, value):
        if "://" in value:
            super().__call__(value)


class Monitor(models.Model):
    # Fields ordered for optimal data alignment: 8-byte (FKs, timestamps), 4-byte, 2-byte, then variable-width
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    environment = models.ForeignKey(
        "environments.Environment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created = models.DateTimeField(auto_now_add=True)
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
    expected_status = models.PositiveSmallIntegerField(
        default=200, blank=True, null=True
    )
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
    expected_body = models.CharField(max_length=2000, blank=True)
    cached_is_up = models.BooleanField(null=True, default=None)
    cached_last_change = models.DateTimeField(null=True, default=None)

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
        from django.db import transaction

        from apps.uptime.tasks import perform_checks

        if self.monitor_type != MonitorType.HEARTBEAT:
            transaction.on_commit(lambda: perform_checks.enqueue([self.pk]))

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


def _generate_uuid7():
    """Generate UUIDv7 for MonitorCheck default."""
    return UUID7Helper.from_datetime()


class MonitorCheck(models.Model):
    # Partitioned by id (UUIDv7) -> Hash by organization

    # 16-byte alignment: UUIDs
    id = models.UUIDField(
        default=_generate_uuid7,
        editable=False,
    )
    # Primary Key is composite (id, organization)
    pk = models.CompositePrimaryKey("id", "organization")

    # 8-byte alignment
    monitor = models.ForeignKey(
        Monitor, on_delete=models.CASCADE, related_name="checks"
    )
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    start_check = models.DateTimeField(
        default=now,
        help_text="Time when the start of this check was performed",
    )

    # Other fields
    response_time = models.PositiveIntegerField(
        blank=True, null=True, help_text="Reponse time in milliseconds"
    )
    reason = models.PositiveSmallIntegerField(
        choices=MonitorCheckReason.choices, default=0, null=True, blank=True
    )
    is_up = models.BooleanField()
    is_change = models.BooleanField(
        help_text="Indicates change to is_up status for associated monitor",
    )
    data = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["monitor", "-start_check"]),
            models.Index(fields=["monitor", "is_change", "-start_check"]),
        ]
        ordering = ("-start_check",)

    def __str__(self):
        return self.up_or_down

    @property
    def up_or_down(self):
        if self.is_up:
            return "Up"
        return "Down"


class UptimeCheckHourlyStatistic(models.Model):
    """
    Hourly uptime check counts per organization.
    Weekly range partitioned by date (like other ProjectStatisticBase tables).
    """

    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    date = models.DateTimeField()
    count = models.PositiveIntegerField()
    pk = models.CompositePrimaryKey("organization", "date")

    class Meta:
        pass


class StatusPage(CreatedModel):
    """
    A status page is a collection of monitors that are available to view
    """

    # Fields ordered for optimal data alignment: 8-byte FKs first, 1-byte booleans, then variable-width
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    is_public = models.BooleanField(
        help_text="When true, the status page URL is publicly accessible"
    )
    name = models.CharField(max_length=200)
    slug = AutoSlugField(populate_from=["name"], max_length=200)
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
