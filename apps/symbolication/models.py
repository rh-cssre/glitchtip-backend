from django.db import models


class CacheKind(models.IntegerChoices):
    SOURCEMAP = 1, "sourcemap"
    SYMCACHE = 2, "symcache"
    PROGUARD = 3, "proguard"


class CacheState(models.IntegerChoices):
    # Requested by an event; a build task is queued.
    PENDING = 1
    # A worker claimed the build (see ``claimed``).
    BUILDING = 2
    READY = 3
    # The source cannot be turned into a cache with this format; not retried
    # until the format changes.
    FAILED = 4


class SymbolicationCache(models.Model):
    """A built lookup cache for one uploaded symbolication artifact.

    Keyed by the content of its source, so every upload of the same bytes
    shares one cache. The cache bytes live in the default file storage under
    ``storage_name``. When a source FileBlob is deleted its column goes NULL,
    and maintenance deletes the row and its stored cache.

    Columns are ordered for alignment: 8-byte, then 4-, then 2-byte, then
    variable length.
    """

    id = models.BigAutoField(primary_key=True)
    created = models.DateTimeField(auto_now_add=True)
    # When the state last changed; a stale BUILDING claim can be taken over.
    updated = models.DateTimeField(auto_now=True)
    # DB_SET_NULL: the database clears these when a FileBlob is deleted, so
    # FileBlob deletes keep working from code that does not know this model
    # (an older release during a rolling deploy or after a downgrade).
    source_blob = models.ForeignKey(
        "files.FileBlob",
        on_delete=models.DB_SET_NULL,
        null=True,
        related_name="+",
    )
    # The sourcemap, for a sourcemap cache (``source_blob`` is the minified
    # file).
    secondary_blob = models.ForeignKey(
        "files.FileBlob",
        on_delete=models.DB_SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # The organization whose event asked for the build, to bound how many
    # builds one organization can have outstanding. Not a foreign key: the
    # cache is shared by content, and outlives the organization.
    requested_by_organization_id = models.BigIntegerField(null=True, blank=True)
    size = models.PositiveIntegerField(default=0)
    kind = models.PositiveSmallIntegerField(choices=CacheKind.choices)
    state = models.PositiveSmallIntegerField(
        choices=CacheState.choices, default=CacheState.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    # Identifies the source: blob checksums, plus the object for a native
    # file (debug id and arch).
    cache_key = models.TextField()
    # gt_rust's cache_format_tag(kind) at build time. A gt_rust upgrade that
    # changes the format makes every old row a miss.
    format_tag = models.TextField()
    storage_name = models.TextField(blank=True, default="")
    error = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "cache_key", "format_tag"],
                name="symbolication_cache_unique_key",
            ),
        ]
        indexes = [
            # Outstanding builds per organization (store._request_build).
            models.Index(
                fields=["requested_by_organization_id"],
                name="symbolication_cache_pending",
                condition=models.Q(state__in=[CacheState.PENDING, CacheState.BUILDING]),
            ),
        ]
