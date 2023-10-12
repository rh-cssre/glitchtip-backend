import random
from datetime import timedelta
from urllib.parse import urlparse
from uuid import uuid4

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Count, Q
from django.utils.text import slugify
from django_extensions.db.fields import AutoSlugField

from glitchtip.base_models import CreatedModel
from observability.metrics import clear_metrics_cache


class Project(CreatedModel):
    """
    Projects are permission based namespaces which generally
    are the top level entry point for all data.
    """

    slug = AutoSlugField(populate_from=["name", "organization_id"], max_length=50)
    name = models.CharField(max_length=64)
    organization = models.ForeignKey(
        "organizations_ext.Organization",
        on_delete=models.CASCADE,
        related_name="projects",
    )
    platform = models.CharField(max_length=64, blank=True, null=True)
    first_event = models.DateTimeField(null=True)
    scrub_ip_addresses = models.BooleanField(
        default=True,
        help_text="Should project anonymize IP Addresses",
    )
    event_throttle_rate = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(100)],
        help_text="Probability (in percent) on how many events are throttled. Used for throttling at project level",
    )

    class Meta:
        unique_together = (("organization", "slug"),)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        first = False
        if not self.pk:
            first = True
        super().save(*args, **kwargs)
        if first:
            clear_metrics_cache()
            ProjectKey.objects.create(project=self)

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        clear_metrics_cache()

    @property
    def should_scrub_ip_addresses(self):
        """Organization overrides project setting"""
        return self.scrub_ip_addresses or self.organization.scrub_ip_addresses

    def slugify_function(self, content):
        """
        Make the slug the project name. Validate uniqueness with both name and org id.
        This works because when it runs on organization_id it returns an empty string.
        """
        reserved_words = ["new"]

        slug = ""
        if isinstance(content, str):
            slug = slugify(self.name)
            if slug in reserved_words:
                slug += "-1"
        return slug

    @property
    def is_accepting_events(self):
        """Is the project in its limits for event creation"""
        if self.event_throttle_rate == 0:
            return True
        return random.randint(0, 100) > self.event_throttle_rate


class ProjectCounter(models.Model):
    """
    Counter for issue short IDs
    - Unique per project
    - Autoincrements on each new issue
    - Separate table for performance
    """

    project = models.OneToOneField(Project, on_delete=models.CASCADE)
    value = models.PositiveIntegerField()


class ProjectKey(CreatedModel):
    """Authentication key for a Project"""

    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    label = models.CharField(max_length=64, blank=True)
    public_key = models.UUIDField(default=uuid4, unique=True, editable=False)
    rate_limit_count = models.PositiveSmallIntegerField(blank=True, null=True)
    rate_limit_window = models.PositiveSmallIntegerField(blank=True, null=True)
    data = models.JSONField(blank=True, null=True)

    def __str__(self):
        return str(self.public_key)

    @classmethod
    def from_dsn(cls, dsn: str):
        urlparts = urlparse(dsn)

        public_key = urlparts.username
        project_id = urlparts.path.rsplit("/", 1)[-1]

        try:
            return ProjectKey.objects.get(public_key=public_key, project=project_id)
        except ValueError as err:
            # ValueError would come from a non-integer project_id,
            # which is obviously a DoesNotExist. We catch and rethrow this
            # so anything downstream expecting DoesNotExist works fine
            raise ProjectKey.DoesNotExist(
                "ProjectKey matching query does not exist."
            ) from err

    @property
    def public_key_hex(self):
        """The public key without dashes"""
        return self.public_key.hex

    def dsn(self):
        return self.get_dsn()

    def get_dsn(self):
        urlparts = settings.GLITCHTIP_URL

        # If we do not have a scheme or domain/hostname, dsn is never valid
        if not urlparts.netloc or not urlparts.scheme:
            return ""

        return "%s://%s@%s/%s" % (
            urlparts.scheme,
            self.public_key_hex,
            urlparts.netloc + urlparts.path,
            self.project_id,
        )

    def get_dsn_security(self):
        urlparts = settings.GLITCHTIP_URL

        if not urlparts.netloc or not urlparts.scheme:
            return ""

        return "%s://%s/api/%s/security/?glitchtip_key=%s" % (
            urlparts.scheme,
            urlparts.netloc + urlparts.path,
            self.project_id,
            self.public_key_hex,
        )


class ProjectStatisticBase(models.Model):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    date = models.DateTimeField()
    count = models.PositiveIntegerField()

    class Meta:
        unique_together = (("project", "date"),)
        abstract = True

    @classmethod
    def update(cls, project_id: int, start_time: "datetime"):
        """
        Update current hour and last hour statistics
        start_time should be the time of the last known event creation
        This method recalculates both stats, replacing any previous entry
        """
        current_hour = start_time.replace(second=0, microsecond=0, minute=0)
        next_hour = current_hour + timedelta(hours=1)
        previous_hour = current_hour - timedelta(hours=1)
        projects = Project.objects.filter(pk=project_id)
        event_counts = cls.aggregate_queryset(
            projects, previous_hour, current_hour, next_hour
        )
        statistics = []
        if event_counts["previous_hour_count"]:
            statistics.append(
                cls(
                    project_id=project_id,
                    date=previous_hour,
                    count=event_counts["previous_hour_count"],
                )
            )
        if event_counts["current_hour_count"]:
            statistics.append(
                cls(
                    project_id=project_id,
                    date=current_hour,
                    count=event_counts["current_hour_count"],
                )
            )
        if statistics:
            cls.objects.bulk_create(
                statistics,
                update_conflicts=True,
                unique_fields=["project", "date"],
                update_fields=["count"],
            )


class TransactionEventProjectHourlyStatistic(ProjectStatisticBase):
    @classmethod
    def aggregate_queryset(
        cls,
        project_queryset,
        previous_hour: "datetime",
        current_hour: "datetime",
        next_hour: "datetime",
    ):
        # Redundant filter optimization - otherwise all rows are scanned
        return project_queryset.filter(
            transactiongroup__transactionevent__created__gte=previous_hour,
            transactiongroup__transactionevent__created__lt=next_hour,
        ).aggregate(
            previous_hour_count=Count(
                "transactiongroup__transactionevent",
                filter=Q(
                    transactiongroup__transactionevent__created__gte=previous_hour,
                    transactiongroup__transactionevent__created__lt=current_hour,
                ),
            ),
            current_hour_count=Count(
                "transactiongroup__transactionevent",
                filter=Q(
                    transactiongroup__transactionevent__created__gte=current_hour,
                    transactiongroup__transactionevent__created__lt=next_hour,
                ),
            ),
        )


class EventProjectHourlyStatistic(ProjectStatisticBase):
    @classmethod
    def aggregate_queryset(
        cls,
        project_queryset,
        previous_hour: "datetime",
        current_hour: "datetime",
        next_hour: "datetime",
    ):
        # Redundant filter optimization - otherwise all rows are scanned
        return project_queryset.filter(
            issue__event__created__gte=previous_hour,
            issue__event__created__lt=next_hour,
        ).aggregate(
            previous_hour_count=Count(
                "issue__event",
                filter=Q(
                    issue__event__created__gte=previous_hour,
                    issue__event__created__lt=current_hour,
                ),
            ),
            current_hour_count=Count(
                "issue__event",
                filter=Q(
                    issue__event__created__gte=current_hour,
                    issue__event__created__lt=next_hour,
                ),
            ),
        )


class ProjectAlertStatus(models.IntegerChoices):
    OFF = 0, "off"
    ON = 1, "on"


class UserProjectAlert(models.Model):
    """
    Determine if user alert notifications should always happen, never, or defer to default
    Default is stored as the lack of record.
    """

    user = models.ForeignKey("users.User", on_delete=models.CASCADE)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    status = models.PositiveSmallIntegerField(choices=ProjectAlertStatus.choices)

    class Meta:
        unique_together = ("user", "project")
