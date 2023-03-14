from django.db import models

from glitchtip.base_models import CreatedModel


class Comment(CreatedModel):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    issue = models.ForeignKey("issues.Issue", on_delete=models.CASCADE)
    user = models.ForeignKey("users.User", null=True, on_delete=models.SET_NULL)
    text = models.TextField(blank=True, null=True)
