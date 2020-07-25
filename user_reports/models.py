from django.db import models


class UserReport(models.Model):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    issue = models.ForeignKey("issues.Issue", null=True, on_delete=models.CASCADE)
    event_id = models.UUIDField()
    name = models.CharField(max_length=128)
    email = models.EmailField()
    comments = models.TextField()
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        unique_together = (("project", "event_id"),)
