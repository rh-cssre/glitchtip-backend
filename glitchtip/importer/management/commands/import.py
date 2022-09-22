from django.core.management.base import BaseCommand

from glitchtip.importer.importer import GlitchTipImporter


class Command(BaseCommand):
    help = "Import data from another GlitchTip instance or Sentry"

    def add_arguments(self, parser):
        parser.add_argument("url", type=str)
        parser.add_argument("auth_token", type=str)
        parser.add_argument("organization_slug", type=str)

    def handle(self, *args, **options):
        url = options["url"].rstrip("/")
        if not url.startswith("http"):
            url = "https://" + url
        importer = GlitchTipImporter(
            url, options["auth_token"], options["organization_slug"], create_users=True
        )
        importer.check_auth()
        importer.run()
