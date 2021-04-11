from django.db import models


class CreatedModel(models.Model):
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        abstract = True
