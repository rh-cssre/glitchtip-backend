from django.apps import AppConfig


class SymbolicationConfig(AppConfig):
    name = "apps.symbolication"

    def ready(self):
        from .tasks import build_memory_mb

        # Logs, once, a build budget lowered to fit the container.
        build_memory_mb()
