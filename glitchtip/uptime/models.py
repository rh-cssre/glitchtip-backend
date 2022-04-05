import uuid
from datetime import timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models import OuterRef, Subquery

from glitchtip.base_models import CreatedModel

from .constants import MonitorCheckReason, MonitorType


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
                MonitorCheck.objects.filter(monitor_id=OuterRef("id"))
                .order_by("-start_check")
                .values("is_up")[:1]
            ),
            last_change=Subquery(
                MonitorCheck.objects.filter(monitor_id=OuterRef("id"))
                .exclude(is_up=OuterRef("latest_is_up"))
                .order_by("-start_check")
                .values("start_check")[:1]
            ),
        )


class Monitor(CreatedModel):
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
    url = models.URLField(max_length=2000, blank=True)
    expected_status = models.PositiveSmallIntegerField(default=200)
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
    interval = models.DurationField(
        default=timedelta(minutes=1),
        validators=[MaxValueValidator(timedelta(hours=23, minutes=59, seconds=59))],
    )

    objects = MonitorManager()

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.monitor_type == MonitorType.HEARTBEAT and not self.endpoint_id:
            self.endpoint_id = uuid.uuid4()
        super().save(*args, **kwargs)
        # pylint: disable=import-outside-toplevel
        from glitchtip.uptime.tasks import perform_checks

        if self.monitor_type != MonitorType.HEARTBEAT:
            perform_checks.apply_async(args=([self.pk],), countdown=1)

    def get_detail_url(self):
        return f"{settings.GLITCHTIP_URL.geturl()}/{self.project.organization.slug}/uptime-monitors/{self.pk}"


class MonitorCheck(CreatedModel):
    monitor = models.ForeignKey(
        Monitor, on_delete=models.CASCADE, related_name="checks"
    )
    is_up = models.BooleanField()
    start_check = models.DateTimeField(
        help_text="Time when the start of this check was performed"
    )
    reason = models.PositiveSmallIntegerField(
        choices=MonitorCheckReason.choices, default=0, null=True, blank=True
    )
    response_time = models.DurationField(blank=True, null=True)
    data = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["start_check", "monitor"]),
        ]
        ordering = ("-created",)

    def __str__(self):
        return self.up_or_down

    @property
    def up_or_down(self):
        if self.is_up:
            return "Up"
        return "Down"
