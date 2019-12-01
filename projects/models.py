from urllib.parse import urlparse
from uuid import uuid4
from django.contrib.postgres.fields import JSONField
from django.conf import settings
from django.db import models
from django_extensions.db.fields import AutoSlugField
from organizations.models import Organization


class Project(models.Model):
    """
    Projects are permission based namespaces which generally
    are the top level entry point for all data.
    """

    slug = AutoSlugField(populate_from=["name"])
    name = models.CharField(max_length=200)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE
    )
    date_added = models.DateTimeField(auto_now_add=True)
    platform = models.CharField(max_length=64, null=True)

    class Meta:
        unique_together = (("organization", "slug"),)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.organization_id:  # Temp thing
            self.organization = Organization.objects.get_or_create(name="test org")[0]
        super().save(*args, **kwargs)
        ProjectKey.objects.create(project=self)


class ProjectKey(models.Model):
    """ Authentication key for a Project """

    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    label = models.CharField(max_length=64, blank=True)
    public_key = models.CharField(max_length=32, unique=True)
    date_added = models.DateTimeField(auto_now_add=True)
    rate_limit_count = models.PositiveSmallIntegerField(blank=True, null=True)
    rate_limit_window = models.PositiveSmallIntegerField(blank=True, null=True)
    data = JSONField(blank=True, null=True)

    def __str__(self):
        return self.public_key

    def save(self, *args, **kwargs):
        if not self.id:
            self.public_key = ProjectKey.generate_api_key()
        super().save(*args, **kwargs)

    @classmethod
    def generate_api_key(cls):
        return uuid4().hex

    @classmethod
    def from_dsn(cls, dsn):
        urlparts = urlparse(dsn)

        public_key = urlparts.username
        project_id = urlparts.path.rsplit("/", 1)[-1]

        try:
            return ProjectKey.objects.get(public_key=public_key, project=project_id)
        except ValueError:
            # ValueError would come from a non-integer project_id,
            # which is obviously a DoesNotExist. We catch and rethrow this
            # so anything downstream expecting DoesNotExist works fine
            raise ProjectKey.DoesNotExist("ProjectKey matching query does not exist.")

    def dsn(self):
        return self.get_dsn()

    def get_dsn(self):
        key = self.public_key
        urlparts = settings.GLITCHTIP_ENDPOINT

        # If we do not have a scheme or domain/hostname, dsn is never valid
        if not urlparts.netloc or not urlparts.scheme:
            return ""

        return "%s://%s@%s/%s" % (
            urlparts.scheme,
            key,
            urlparts.netloc + urlparts.path,
            self.project_id,
        )
