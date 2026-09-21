from django.db import models

from glitchtip.base_models import CreatedModel


class RepositoryStatus(models.TextChoices):
    ACTIVE = "active"
    DISABLED = "disabled"
    HIDDEN = "hidden"
    PENDING_DELETION = "pending_deletion"
    DELETION_IN_PROGRESS = "deletion_in_progress"


class Repository(CreatedModel):
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=200)
    url = models.URLField(blank=True, default="")
    provider = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=24,
        choices=RepositoryStatus.choices,
        default=RepositoryStatus.ACTIVE,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="sourcecode_repository_unique_org_name",
            ),
        ]

    def __str__(self):
        return self.name


class DebugSymbolBundle(CreatedModel):
    """
    Supports Artifact Bundles, Release Bundles, and DIFs
    """

    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    debug_id = models.UUIDField(blank=True, null=True)
    last_used = models.DateTimeField(auto_now=True, db_index=True)
    release = models.ForeignKey(
        "releases.release",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )
    sourcemap_file = models.ForeignKey(
        "files.File", on_delete=models.SET_NULL, blank=True, null=True, related_name="+"
    )
    file = models.ForeignKey("files.File", on_delete=models.CASCADE)
    data = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "debug_id"], name="unique_org_debug_id"
            ),
            models.UniqueConstraint(
                fields=["release", "file"], name="unique_release_file"
            ),
            models.CheckConstraint(
                condition=models.Q(debug_id__isnull=False)
                | models.Q(release__isnull=False),
                name="debug_id_or_release_required",
            ),
        ]
