from django.db import models


class ArtifactBundle(models.Model):
    debug_id = models.UUIDField(primary_key=True)
    last_used = models.DateTimeField(auto_now=True, db_index=True)
    release = models.ForeignKey(
        "releases.release", on_delete=models.SET_NULL, blank=True, null=True
    )
    minified_file = models.ForeignKey(
        "files.File", on_delete=models.CASCADE, related_name="+"
    )
    sourcemap_file = models.ForeignKey(
        "files.File", on_delete=models.CASCADE, related_name="+"
    )
