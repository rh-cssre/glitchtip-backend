from django.apps import AppConfig


class UsersConfig(AppConfig):
    name = 'apps.users'

    def ready(self):
        import apps.users.signals  # noqa: F401 #This import registers the listener
