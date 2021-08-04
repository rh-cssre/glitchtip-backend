from datetime import timedelta
from django.db import models
from glitchtip.base_models import CreatedModel
from .constants import MonitorType, MonitorCheckReason


class Monitor(CreatedModel):
    monitor_type = models.CharField(
        max_length=12, choices=MonitorType.choices, default=MonitorType.PING
    )
    name = models.CharField(max_length=200)
    url = models.URLField(blank=True)
    expected_status = models.PositiveSmallIntegerField(default=200)
    expected_body = models.CharField(max_length=2000, blank=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    environment = models.ForeignKey(
        "environments.Environment", on_delete=models.SET_NULL, null=True, blank=True,
    )
    project = models.ForeignKey(
        "projects.Project", on_delete=models.SET_NULL, null=True, blank=True,
    )
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    interval = models.DurationField(default=timedelta(minutes=1))

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from uptime.tasks import perform_checks

        perform_checks.apply_async(args=([self.pk],), countdown=1)


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
        return f"{self.monitor}: {self.up_or_down}"

    @property
    def up_or_down(self):
        if self.is_up:
            return "Up"
        return "Down"
