from django.db.migrations.operations.models import CreateModel


class PostgresCreatePartitionedModel(CreateModel):
    def __init__(self, *args, **kwargs):
        if "partitioning_options" in kwargs:
            kwargs.pop("partitioning_options")
        super().__init__(*args, **kwargs)
