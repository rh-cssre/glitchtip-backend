from django.db import models
from django.db.models.fields.json import KeyTransform

from glitchtip.base_models import CreatedModel


class DebugInformationFile(CreatedModel):
    """
    It hold info of the uploaded debug information file
    """

    name = models.TextField()
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    file = models.ForeignKey("files.File", on_delete=models.CASCADE)
    data = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["project", "file"]),
            # Symbolication looks debug files up by project and debug id
            # (data->'debug_id', matching the data__debug_id lookups).
            models.Index(
                models.F("project"),
                KeyTransform("debug_id", "data"),
                name="difs_project_debug_id",
            ),
        ]

    def is_proguard_mapping(self):
        try:
            return self.data["symbol_type"] == "proguard"
        except Exception:
            return False
