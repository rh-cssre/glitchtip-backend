from django.db import models


class EnvironmentProject(models.Model):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    environment = models.ForeignKey(
        "environments.Environment", on_delete=models.CASCADE
    )
    is_hidden = models.BooleanField()
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        unique_together = ("project", "environment")


class Environment(models.Model):
    name = models.CharField(max_length=256)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    projects = models.ManyToManyField("projects.Project", through=EnvironmentProject)
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        unique_together = ("organization", "name")

    def __str__(self):
        return self.name
