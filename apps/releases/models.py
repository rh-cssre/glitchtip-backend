from hashlib import sha1

from django.db import models

from glitchtip.base_models import CreatedModel


class Release(CreatedModel):
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    projects = models.ManyToManyField("projects.Project", related_name="releases")
    version = models.CharField(max_length=255)
    ref = models.CharField(
        max_length=255, null=True, blank=True, help_text="May be branch or tag name"
    )
    url = models.URLField(null=True, blank=True)
    released = models.DateTimeField(null=True, blank=True)
    data = models.JSONField(default=dict)
    owner = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="Release manager - the person initiating the release",
    )
    commit_count = models.PositiveSmallIntegerField(default=0)
    # last commit - not implemented
    # authors - not implemented
    deploy_count = models.PositiveSmallIntegerField(default=0)
    # last_deploy - not implemented
    files = models.ManyToManyField("files.File", through="ReleaseFile")

    class Meta:
        unique_together = ("organization", "version")


class ReleaseProject(models.Model):
    """Through model may be used to store cached event counts in the future"""

    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    release = models.ForeignKey(Release, on_delete=models.CASCADE)

    class Meta:
        unique_together = ("project", "release")


class ReleaseFile(CreatedModel):
    release = models.ForeignKey(Release, on_delete=models.CASCADE)
    file = models.ForeignKey("files.File", on_delete=models.CASCADE)
    ident = models.CharField(max_length=40)
    name = models.TextField()

    class Meta:
        unique_together = (("release", "file"), ("release", "ident"))

    def save(self, *args, **kwargs):
        if not self.ident:
            self.ident = type(self).get_ident(self.name)
        return super().save(*args, **kwargs)

    @classmethod
    def get_ident(cls, name, dist=None):
        if dist is not None:
            dist_name = name + "\x00\x00" + dist
            return sha1(dist_name.encode()).hexdigest()
        return sha1(name.encode()).hexdigest()
