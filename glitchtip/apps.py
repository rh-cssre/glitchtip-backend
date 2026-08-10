from django.apps import AppConfig


class GlitchtipConfig(AppConfig):
    name = "glitchtip"

    def ready(self):
        from . import task_signals  # noqa: F401

        self._skip_hstore_oid_lookup()

    @staticmethod
    def _skip_hstore_oid_lookup():
        # django.contrib.postgres wires register_type_handlers to
        # connection_created, which runs `SELECT ... FROM pg_type WHERE
        # typname = 'hstore'` on every new connection so psycopg can
        # auto-decode hstore columns. We use jsonb instead of hstore, so
        # the round-trip is pure overhead — and the receiver uses the
        # sync ORM, which raises SynchronousOnlyOperation when a task
        # opens its first async connection.
        from django.contrib.postgres.signals import register_type_handlers
        from django.db.backends.signals import connection_created

        connection_created.disconnect(register_type_handlers)
