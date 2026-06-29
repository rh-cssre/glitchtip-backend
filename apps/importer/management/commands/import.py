from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand

from apps.importer.importer import GlitchTipImporter


class Command(BaseCommand):
    help = "Import data from another GlitchTip instance or Sentry"

    def add_arguments(self, parser):
        parser.add_argument("url", type=str)
        parser.add_argument("auth_token", type=str)
        parser.add_argument("organization_slug", type=str)

    @async_to_sync
    async def handle(self, *args, **options):
        url = options["url"].rstrip("/")
        if not url.startswith("http"):
            url = "https://" + url
        importer = GlitchTipImporter(
            url, options["auth_token"], options["organization_slug"], create_users=True
        )
        await importer.check_auth()
        await importer.run()
