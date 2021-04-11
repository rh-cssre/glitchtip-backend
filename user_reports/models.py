from django.db import models
from glitchtip.base_models import CreatedModel


class UserReport(CreatedModel):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    issue = models.ForeignKey("issues.Issue", null=True, on_delete=models.CASCADE)
    event_id = models.UUIDField()
    name = models.CharField(max_length=128)
    email = models.EmailField()
    comments = models.TextField()

    class Meta:
        unique_together = (("project", "event_id"),)
