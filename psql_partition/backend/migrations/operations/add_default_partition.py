from django.db.migrations.operations.base import Operation


class PostgresAddDefaultPartition(Operation):
    def __init__(self, *args, **kwargs):
        pass

    def state_forwards(self, *args, **kwargs):
        pass

    def database_forwards(self, *args, **kwargs):
        pass

    def database_backwards(self, *args, **kwargs):
        pass
