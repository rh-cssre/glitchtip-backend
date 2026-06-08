from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef

from apps.files.models import File, FileBlob

BATCH_SIZE = 1000


class Command(BaseCommand):
    help = "Delete FileBlob rows (and their backing storage files) that have no File referencing them"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be deleted without making changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        orphaned_qs = FileBlob.objects.filter(
            ~Exists(File.objects.filter(blob=OuterRef("pk")))
        )

        if dry_run:
            total = orphaned_qs.count()
            self.stdout.write(f"[DRY RUN] Would delete {total} orphaned FileBlobs.")
            return

        deleted = 0
        batch = list(
            orphaned_qs.values_list("id", "blob", named=True)[:BATCH_SIZE]
        )
        while batch:
            for row in batch:
                if row.blob:
                    try:
                        default_storage.delete(row.blob)
                    except Exception as e:
                        self.stderr.write(
                            f"Warning: failed to delete storage for FileBlob {row.id}: {e}"
                        )
            FileBlob.objects.filter(
                id__in=[row.id for row in batch]
            ).delete()

            deleted += len(batch)
            self.stdout.write(f"Deleted {deleted} orphaned FileBlobs so far...")
            batch = list(
                orphaned_qs.values_list("id", "blob", named=True)[:BATCH_SIZE]
            )

        if deleted:
            self.stdout.write(
                self.style.SUCCESS(f"Done. Deleted {deleted} orphaned FileBlobs.")
            )
        else:
            self.stdout.write("No orphaned FileBlobs found.")
