from django.contrib.postgres.fields import JSONField
from django.conf import settings
from django.db import models
from django.template.defaultfilters import slugify
from uuid import uuid4


class Project(models.Model):
    """
    Projects are permission based namespaces which generally
    are the top level entry point for all data.
    """

    slug = models.SlugField()
    name = models.CharField(max_length=200)
    date_added = models.DateTimeField(auto_now_add=True)
    platform = models.CharField(max_length=64, null=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.id:
            # TODO make unique per project
            self.slug = slugify(self.name)
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
