import hashlib

from django.db import models

from glitchtip.partition_manager import UUID7Helper

from .constants import LogLevel


# Cardinality limiter for user-controlled strings in the hourly statistics table.
#
# LogProjectHourlyStatistic has a composite PK:
#   (project, organization, date, level, service_bucket, environment_bucket)
#
# Without bucketing, each unique name would create its own rows.
# If names are high-cardinality (e.g. auto-generated, UUIDs, or
# per-request), the stats table would grow unboundedly. Hashing into 256
# buckets caps the worst case at 256^2 * 6 levels * 24 hours = ~9.4M rows
# per project per day, regardless of how many distinct values exist.
#
# Tradeoff: filtering stats by name may include collisions from
# other values that hash to the same bucket. This is acceptable for
# aggregate charts — exact per-value counts come from the logs table.
def compute_hash_bucket(name: str) -> int:
    """
    Hash a string to a bucket (0-255) for statistics aggregation.

    Used by LogProjectHourlyStatistic to bound row count when
    user-controlled strings (service, environment) have high cardinality.
    The stats API filters by bucket, so queries like "show me stats for
    auth-service" hash the name and filter on the bucket. Collisions are
    rare with typical value counts and acceptable for aggregate charts.
    """
    if not name:
        return 0
    digest = hashlib.md5(name.encode(), usedforsecurity=False).digest()
    return digest[0]  # First byte gives 0-255


class LogEvent(models.Model):
    """
    Log event storage with optimized column alignment.

    Column alignment (reduces padding, improves CPU cache):
    - 16-byte: UUIDs (id, trace_id)
    - 8-byte: ForeignKeys and BigInt (organization, project, span_id)
    - 2-byte: SmallIntegers (level, severity_number)
    - Variable: Text/JSON fields (body, service, data)

    Timestamp is derived from the UUIDv7 id field which encodes millisecond-precise
    client timestamp. No separate timestamp column - the id IS the timestamp.

    Partitioning:
    - Uses native Python PartitionManager
    - Partitioned by RANGE on id (UUIDv7)
    - Sub-partitioned by HASH on organization_id
    """

    # 16-byte alignment: UUIDs
    id = models.UUIDField(
        editable=False,
        help_text="UUIDv7 generated from client timestamp (partition key)",
    )
    # Primary Key is composite (id, organization) to allow HASH sub-partitioning
    pk = models.CompositePrimaryKey("id", "organization")

    trace_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="Trace ID for correlating logs with traces",
    )

    # 8-byte alignment: ForeignKeys and BigInt
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    span_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Span ID for correlating logs with specific spans (8-byte)",
    )

    # 2-byte alignment
    level = models.PositiveSmallIntegerField(
        choices=LogLevel.choices,
        default=LogLevel.INFO,
        help_text="Log level (trace/debug/info/warn/error/fatal)",
    )
    severity_number = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="OpenTelemetry severity number (1-24)",
    )

    # Variable-width fields
    body = models.TextField(
        help_text="The log message body",
    )
    service = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Service name that emitted the log",
    )
    environment = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Deployment environment (e.g. production, staging)",
    )
    host = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Host name that emitted the log",
    )
    data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional structured data/attributes",
    )

    class Meta:
        indexes = [
            # Primary query pattern: list logs for org, ordered by time
            models.Index(fields=["organization", "-id"], name="logevent_org_id_idx"),
            # Filter by project within org
            models.Index(fields=["project", "-id"], name="logevent_proj_id_idx"),
            # Filter by level
            models.Index(
                fields=["organization", "level", "-id"], name="logevent_org_level_idx"
            ),
            # Filter by service
            models.Index(
                fields=["organization", "service", "-id"], name="logevent_org_svc_idx"
            ),
            # Filter by environment
            models.Index(
                fields=["organization", "environment", "-id"],
                name="logevent_org_env_idx",
            ),
            # Filter by host
            models.Index(
                fields=["organization", "host", "-id"], name="logevent_org_host_idx"
            ),
            # Trace correlation
            models.Index(
                fields=["trace_id"],
                name="logevent_trace_id_idx",
                condition=models.Q(trace_id__isnull=False),
            ),
        ]

    def __str__(self):
        return f"[{self.get_level_display()}] {self.body[:50]}"

    @property
    def timestamp(self):
        """
        Time at which log was emitted by the application.

        Derived from UUIDv7 id which encodes millisecond-precision timestamp
        from the client's reported event time.
        """
        return UUID7Helper.extract_datetime(self.id)


class LogResource(models.Model):
    """
    Lookup table for unique resource names (service, environment, host) per organization.

    This allows the UI to show dropdowns of known values without
    querying the large logs table. Updated during log ingestion.
    """

    class ResourceType(models.TextChoices):
        SERVICE = "service", "service"
        ENVIRONMENT = "environment", "environment"
        HOST = "host", "host"

    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=255, help_text="Resource name")
    type = models.CharField(
        max_length=20,
        choices=ResourceType.choices,
        default=ResourceType.SERVICE,
        help_text="Type of resource (service/environment/host)",
    )
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name", "type"], name="unique_org_resource"
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "type"], name="logresource_org_type_idx"
            ),
        ]

    def __str__(self):
        return f"{self.type}: {self.name}"
