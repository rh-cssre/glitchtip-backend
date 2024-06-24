from django.db import models

from apps.organizations_ext.models import Organization
from glitchtip.base_models import CreatedModel


class Team(CreatedModel):
    slug = models.SlugField()
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="teams"
    )
    members = models.ManyToManyField(
        "organizations_ext.OrganizationUser", blank=True, related_name="teams"
    )
    projects = models.ManyToManyField("projects.Project", related_name="teams")

    class Meta:
        unique_together = ("slug", "organization")

    def __str__(self):
        return self.slug
