import time

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q

from app.models import Item, MediaTypes
from app.tasks import download_item_poster

PROGRESS_EVERY = 50


class Command(BaseCommand):
    """Backfill locally cached posters for items added before caching existed.

    Automatic caching only triggers when an item is added to or updated in a
    list, so libraries built earlier keep hotlinking the remote CDN. This
    command finds tracked items that still lack a local copy and downloads
    them, either by queueing Celery tasks (default) or synchronously.
    """

    help = "Download and cache posters for tracked items missing a local copy."

    def add_arguments(self, parser):
        """Register command-line options."""
        parser.add_argument(
            "--all",
            action="store_true",
            dest="include_all",
            help="Include items not referenced by any tracked entry.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of posters to process.",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Download in-process instead of queueing Celery tasks.",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.0,
            help="Seconds to wait between downloads in --sync mode (throttling).",
        )

    def handle(self, *args, **options):  # noqa: ARG002
        """Find items missing a cached poster and download them."""
        if not settings.DOWNLOAD_POSTERS:
            self.stderr.write(
                self.style.WARNING(
                    "DOWNLOAD_POSTERS is disabled; nothing will be downloaded. "
                    "Set DOWNLOAD_POSTERS=True to enable poster caching.",
                ),
            )
            return

        item_ids = self._collect_item_ids(
            include_all=options["include_all"],
            limit=options["limit"],
        )
        if not item_ids:
            self.stdout.write("No items need a cached poster.")
            return

        self._process(item_ids, sync=options["sync"], sleep_seconds=options["sleep"])

    def _collect_item_ids(self, *, include_all, limit):
        """Return ids of items that still need a locally cached poster."""
        queryset = (
            Item.objects.filter(Q(image_local__isnull=True) | Q(image_local=""))
            .exclude(image="")
            .exclude(image=settings.IMG_NONE)
        )

        if not include_all:
            tracked_ids = set()
            for media_type in MediaTypes.values:
                model = apps.get_model(app_label="app", model_name=media_type)
                tracked_ids.update(model.objects.values_list("item_id", flat=True))
            queryset = queryset.filter(pk__in=tracked_ids)

        ids_queryset = queryset.order_by("pk").values_list("pk", flat=True)
        if limit is not None:
            ids_queryset = ids_queryset[:limit]
        return list(ids_queryset)

    def _process(self, item_ids, *, sync, sleep_seconds):
        """Download or queue posters for the given item ids."""
        total = len(item_ids)
        verb = "Downloading" if sync else "Queueing"
        self.stdout.write(f"{verb} posters for {total} item(s)...")

        for index, item_id in enumerate(item_ids, start=1):
            if sync:
                download_item_poster(item_id)
                if sleep_seconds:
                    time.sleep(sleep_seconds)
            else:
                download_item_poster.delay(item_id)

            if index % PROGRESS_EVERY == 0:
                self.stdout.write(f"  {index}/{total}")

        if sync:
            self.stdout.write(self.style.SUCCESS(f"Done. Processed {total} item(s)."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Queued {total} download task(s); they will run on the "
                    "Celery worker.",
                ),
            )
