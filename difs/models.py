from django.db import models
from glitchtip.base_models import CreatedModel


class DebugInformationFile(CreatedModel):
    """
    It hold info of the uploaded debug information file
    """

    class Meta:
        indexes = [
            models.Index(fields=["project", "file"])
        ]

    name = models.TextField()

    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)

    file = models.ForeignKey("files.File", on_delete=models.CASCADE)

    data = models.JSONField(null=True, blank=True)

    def is_proguard_mapping(self):
        try:
            return self.data["symbol_type"] == "proguard"
        except Exception:
            return False
