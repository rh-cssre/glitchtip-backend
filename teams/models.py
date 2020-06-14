from django.db import models
from organizations_ext.models import Organization


class Team(models.Model):
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    slug = models.SlugField()
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="teams"
    )
    members = models.ManyToManyField("organizations_ext.OrganizationUser")
    projects = models.ManyToManyField("projects.Project")

    class Meta:
        unique_together = ("slug", "organization")

    def __str__(self):
        return self.slug
