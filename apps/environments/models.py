from django.db import models

from glitchtip.base_models import CreatedModel


class EnvironmentProject(CreatedModel):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    environment = models.ForeignKey(
        "environments.Environment", on_delete=models.CASCADE
    )
    is_hidden = models.BooleanField(default=False)

    class Meta:
        unique_together = ("project", "environment")


class Environment(CreatedModel):
    name = models.CharField(max_length=255)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    projects = models.ManyToManyField("projects.Project", through=EnvironmentProject)

    class Meta:
        unique_together = ("organization", "name")

    def __str__(self):
        return self.name
