from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from glitchtip.base_models import AggregationModel, CreatedModel, SoftDeleteModel
from glitchtip.partition_manager import UUID7Helper
from sentry.constants import MAX_CULPRIT_LENGTH

from .constants import MAX_TAG_LENGTH, EventStatus, IssueEventType, LogLevel
from .managers import EventManager
from .utils import base32_encode


def _generate_uuid7():
    """Generate UUIDv7 for IssueEvent default."""
    return UUID7Helper.from_datetime()


class TagKey(models.Model):
    id = models.AutoField(primary_key=True)
    key = models.CharField(max_length=MAX_TAG_LENGTH, unique=True)


class TagValue(models.Model):
    value = models.CharField(max_length=MAX_TAG_LENGTH)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["value"], name="issue_events_tagvalue_unique_value"
            ),
        ]


class IssueTag(AggregationModel):
    """
    This model is a aggregate of event tags for an issue.
    It is denormalized data that powers fast search results.
    """

    issue = models.ForeignKey("Issue", on_delete=models.CASCADE, db_constraint=False)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    tag_key = models.ForeignKey(TagKey, on_delete=models.CASCADE, db_constraint=False)
    tag_value = models.ForeignKey(
        TagValue, on_delete=models.CASCADE, db_constraint=False
    )
    count = models.PositiveIntegerField(default=1)

    pk = models.CompositePrimaryKey(
        "issue", "organization", "date", "tag_key", "tag_value"
    )

    class Meta:
        pass


class IssueAggregate(AggregationModel):
    """Count the number of events for an issue per time unit"""

    # Fields ordered for optimal data alignment: 8-byte foreign keys first, then other fields
    issue = models.ForeignKey("Issue", on_delete=models.CASCADE, db_constraint=False)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    pk = models.CompositePrimaryKey("issue", "organization", "date")

    # PartitioningMeta removed to detach from psql_partition


class Issue(SoftDeleteModel):
    # Fields ordered for optimal data alignment: 8-byte, 4-byte, 2-byte, 1-byte, then variable-width
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="issues"
    )
    first_release = models.ForeignKey(
        "releases.Release", blank=True, null=True, on_delete=models.SET_NULL
    )
    resolved_in_release = models.ForeignKey(
        "releases.Release",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    assigned_to_org_user = models.ForeignKey(
        "organizations_ext.OrganizationUser",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        db_index=False,
    )
    assigned_to_team = models.ForeignKey(
        "teams.Team",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        db_index=False,
    )
    first_seen = models.DateTimeField(default=timezone.now, db_index=True)
    short_id = models.PositiveIntegerField(null=True)
    type = models.PositiveSmallIntegerField(
        choices=IssueEventType.choices, default=IssueEventType.DEFAULT
    )
    is_public = models.BooleanField(default=False)
    culprit = models.CharField(max_length=1024, blank=True, null=True)
    title = models.CharField(max_length=255)
    metadata = models.JSONField()

    class Meta:
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["project", "short_id"],
                name="project_short_id_unique",
            ),
            models.CheckConstraint(
                name="issue_assigned_to_user_xor_team",
                condition=Q(assigned_to_org_user__isnull=True)
                | Q(assigned_to_team__isnull=True),
            ),
        ]
        indexes = [
            GinIndex(
                fields=["title"],
                name="issue_title_trgm_idx",
                opclasses=["gin_trgm_ops"],
            ),
            models.Index(
                fields=["assigned_to_org_user"],
                condition=Q(assigned_to_org_user__isnull=False),
                name="issue_assigned_org_user_idx",
            ),
            models.Index(
                fields=["assigned_to_team"],
                condition=Q(assigned_to_team__isnull=False),
                name="issue_assigned_team_idx",
            ),
        ]

    # --- Hot-split proxies ---------------------------------------------------
    # count/last_seen/status/level/last_release live on the one-to-one
    # IssueIndex leaf (``index``). These read-only proxies preserve
    # the Issue read API; ORM queries that filter/sort on them must use
    # ``index__<field>`` and any code reading them in bulk should
    # ``select_related("index")`` to avoid an extra query per issue.
    # Writes go through the leaf directly (see process_event / api.issues).
    @property
    def last_seen(self):
        return self.index.last_seen

    @property
    def count(self):
        return self.index.count

    @property
    def status(self):
        return self.index.status

    @property
    def level(self):
        return self.index.level

    @property
    def last_release(self):
        return self.index.last_release

    @property
    def last_release_id(self):
        return self.index.last_release_id

    def get_status_display(self):
        return self.index.get_status_display()

    def get_level_display(self):
        return self.index.get_level_display()

    def __str__(self):
        return self.title

    def get_detail_url(self):
        return f"{settings.GLITCHTIP_URL.geturl()}/{self.project.organization.slug}/issues/{self.pk}"

    def get_hex_color(self):
        if self.level == LogLevel.INFO:
            return "#4b60b4"
        elif self.level is LogLevel.WARNING:
            return "#e9b949"
        elif self.level in [LogLevel.ERROR, LogLevel.FATAL]:
            return "#e52b50"

    @property
    def short_id_display(self):
        """
        Short IDs are per project issue counters. They show as PROJECT_SLUG-ID_BASE32
        The intention is to be human readable identifiers that can reference an issue.
        """
        if self.short_id is not None:
            return f"{self.project.slug.upper()}-{base32_encode(self.short_id)}"
        return ""


class IssueIndex(models.Model):
    """
    Hash-partitioned hot/queryable projection of Issue, decoupled from the main
    Issue table.

    Holds the per-event-updated columns (count, last_seen, last_release) plus
    status/level and the full-text ``fts_document``. Moving these off the wide
    Issue row keeps Issue small and near-static (no per-event rewrite) and gives
    these columns org-partition pruning on the issue-list and search paths.
    ``Issue`` exposes them through proxy properties (``issue.count`` etc.) that
    read this related row, so most readers are unchanged; queries and writes go
    through ``index__*`` / this model directly.

    Fields ordered for alignment (8 / 8 / 8 / 4 / 4 / 2 / 2 then the varlena
    tsvector); see create_issue_index.sql for the packed layout.
    """

    # 8-byte. db_constraint=False because a partitioned table can be neither the
    # target nor (without including the partition key) the source of a DB-level
    # FK; the constraints are intentionally omitted in SQL. Issue rows are
    # pre-deleted in delete_issues_in_batches.
    issue = models.OneToOneField(
        Issue,
        on_delete=models.CASCADE,
        related_name="index",
        db_constraint=False,
    )
    last_release = models.ForeignKey(
        "releases.Release",
        blank=True,
        null=True,
        on_delete=models.DO_NOTHING,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    last_seen = models.DateTimeField(default=timezone.now, editable=False)

    # 4-byte
    organization = models.ForeignKey(
        "organizations_ext.Organization",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
    )
    count = models.PositiveIntegerField(default=1, editable=False)

    # 2-byte. Co-located with last_seen so the default issue-list filter (status)
    # and sort (last_seen) stay single-table on this leaf after the split.
    status = models.PositiveSmallIntegerField(
        choices=EventStatus.choices, default=EventStatus.UNRESOLVED
    )
    level = models.PositiveSmallIntegerField(
        choices=LogLevel.choices, default=LogLevel.ERROR
    )

    pk = models.CompositePrimaryKey("issue", "organization")

    # Variable-width: keep last so the fixed head packs without padding.
    fts_document = SearchVectorField(editable=False, default="")

    class Meta:
        # Only the GIN is declared here (its name is pinned to the raw SQL). The
        # org-scoped btree indexes are created by the raw SQL too but, like the
        # hash partitions, are intentionally not part of Django's model state.
        indexes = [
            GinIndex(fields=["fts_document"], name="issueindex_fts_gin"),
        ]


@receiver(post_save, sender=Issue)
def _ensure_issue_index(instance, created, raw=False, **kwargs):
    """Give every ORM-created Issue its IssueIndex projection row.

    Ingest creates the leaf via raw SQL and does not go through Model.save, so
    the hot write path never hits this. This covers Issues created through the
    ORM (tests, admin, future code), keeping the ``issue.index`` invariant
    that the proxy properties rely on. Idempotent (get_or_create).
    """
    if created and not raw:
        IssueIndex.objects.get_or_create(
            issue_id=instance.id,
            organization_id=instance.project.organization_id,
        )


class IssueHash(models.Model):
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="hashes")
    # Redundant project allows for unique constraint
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="+"
    )
    value = models.UUIDField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "value"], name="issue hash project"
            )
        ]


class Comment(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(
        "users.User", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    text = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ("-created",)


class UserReport(CreatedModel):
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="+"
    )
    issue = models.ForeignKey(Issue, null=True, on_delete=models.CASCADE)
    event_id = models.UUIDField()
    name = models.CharField(max_length=128)
    email = models.EmailField()
    comments = models.TextField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "event_id"],
                name="project_event_unique",
            )
        ]


class IssueEvent(models.Model):
    """
    Dual-ID Schema with Optimized Column Alignment

    Column alignment (reduces padding, improves CPU cache):
    - 16-byte: UUIDs (id, event_id)
    - 8-byte: Timestamps and ForeignKeys (timestamp, received, issue, release, organization)
    - 2-byte: SmallIntegers (type, level)
    - Variable: Text/JSON fields (title, transaction, data, tags, hashes)

    Dual-ID Strategy:
    - id: Server-generated UUIDv7 (partition key, contains timestamp)
    - event_id: Client-provided UUIDv4 (nullable, for SDK compatibility)

    Partitioning:
    - Uses native Python PartitionManager
    - Partitioned by RANGE on id (UUIDv7)
    - Sub-partitioned by HASH on organization_id
    """

    # 16-byte alignment: UUIDs
    id = models.UUIDField(
        default=_generate_uuid7,
        editable=False,
        help_text="Server-generated UUIDv7 (partition key, contains timestamp)",
    )
    # Primary Key is composite (id, organization) to allow HASH sub-partitioning by organization
    pk = models.CompositePrimaryKey("id", "organization")

    event_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="Client-provided event ID from Sentry SDK (UUIDv4)",
    )

    # 8-byte alignment: Timestamps
    timestamp = models.DateTimeField(help_text="Time at which event happened")
    # Note: `received` is now a property derived from UUIDv7 id (millisecond precision)

    # 8-byte alignment: Foreign keys
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, db_constraint=False)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    release = models.ForeignKey(
        "releases.Release", blank=True, null=True, on_delete=models.SET_NULL
    )

    # 2-byte alignment: Small integers
    type = models.PositiveSmallIntegerField(default=0, choices=IssueEventType.choices)
    level = models.PositiveSmallIntegerField(
        choices=LogLevel.choices, default=LogLevel.ERROR
    )

    # Variable-width fields
    title = models.CharField(max_length=255)
    transaction = models.CharField(max_length=MAX_CULPRIT_LENGTH)
    data = models.JSONField()
    tags = models.JSONField()
    hashes = ArrayField(models.TextField(), db_default=[])

    # Use custom manager for smart partition-aware queries
    objects = EventManager()

    class Meta:
        indexes = [
            # Use -id (UUIDv7) for ordering - enables partition pruning
            # `received` is now a property derived from UUIDv7 timestamp
            models.Index(fields=["issue", "-id"], name="issueevent_issue_id_idx"),
            models.Index(fields=["release"], name="issueevent_release_idx"),
            models.Index(
                fields=["event_id"],
                name="issueevent_event_id_idx",
                condition=models.Q(event_id__isnull=False),
            ),
            GinIndex(fields=["hashes"]),
        ]

    def __str__(self):
        return self.eventID

    @property
    def eventID(self):
        """
        Return the event ID for API responses.

        V2 Strategy: Prefer client-provided event_id if available,
        otherwise use server-generated id.
        """
        return (self.event_id or self.id).hex

    @property
    def received(self):
        """
        Time at which GlitchTip accepted the event.

        Derived from UUIDv7 id which encodes millisecond-precision timestamp.
        This replaces the old stored `received` field for V2 storage.
        """
        from glitchtip.partition_manager import UUID7Helper

        return UUID7Helper.extract_datetime(self.id)

    @property
    def message(self):
        """Often the title and message are the same. If message isn't stored, assume it's the title"""
        return self.data.get("message", self.title)

    @property
    def metadata(self):
        """Return metadata if exists, else return just the title as metadata"""
        return self.data.get("metadata", {"title": self.title})

    @property
    def platform(self):
        return self.data.get("platform")
