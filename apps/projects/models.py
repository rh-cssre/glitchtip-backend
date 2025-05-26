from urllib.parse import urlparse
from uuid import uuid4

from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models import Count, Q, QuerySet
from django.db.models.functions import Cast
from django.utils.text import slugify
from django_extensions.db.fields import AutoSlugField

from apps.issue_events.models import Issue, IssueEvent
from apps.observability.utils import clear_metrics_cache
from glitchtip.base_models import AggregationModel, CreatedModel, SoftDeleteModel


class Project(CreatedModel, SoftDeleteModel):
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

    @classmethod
    def annotate_is_member(cls, queryset: QuerySet, user_id: int):
        """Add is_member boolean annotate to Project queryset"""
        return queryset.annotate(
            is_member=Cast(
                Cast(  # Postgres can cast int to bool, but not bigint to bool
                    Count(
                        "teams__members",
                        filter=Q(teams__members__user_id=user_id),
                        distinct=True,
                    ),
                    output_field=models.IntegerField(),
                ),
                output_field=models.BooleanField(),
            ),
        )

    def save(self, *args, **kwargs):
        first = False
        if not self.pk:
            first = True
        super().save(*args, **kwargs)
        if first:
            clear_metrics_cache()
            ProjectKey.objects.create(project=self)

    def delete(self, *args, **kwargs):
        """Mark the record as deleted instead of deleting it"""
        # avoid circular import
        from apps.projects.tasks import delete_project

        super().delete(*args, **kwargs)
        delete_project.delay(self.pk)

    def force_delete(self, *args, **kwargs):
        """Really delete the project and all related data."""
        # bulk delete all events
        events_qs = IssueEvent.objects.filter(issue__project=self)
        events_qs._raw_delete(events_qs.db)

        # bulk delete all issues in batches of 1k
        issues_qs = self.issues.order_by("id")
        while True:
            try:
                issue_delimiter = issues_qs.values_list("id", flat=True)[
                    1000:1001
                ].get()
                issues_qs.filter(id__lte=issue_delimiter).delete()
            except Issue.DoesNotExist:
                break

        issues_qs.delete()

        # lastly delete the project itself
        super().force_delete(*args, **kwargs)
        clear_metrics_cache()

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
    is_active = models.BooleanField(default=True)
    name = models.CharField(max_length=64, blank=True)
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


class ProjectStatisticBase(AggregationModel):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)

    class Meta:
        unique_together = (("project", "date"),)
        abstract = True


class TransactionEventProjectHourlyStatistic(ProjectStatisticBase):
    class PartitioningMeta(AggregationModel.PartitioningMeta):
        pass


class IssueEventProjectHourlyStatistic(ProjectStatisticBase):
    class PartitioningMeta(AggregationModel.PartitioningMeta):
        pass


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
