import logging
from datetime import timedelta

import requests
from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from app import helpers
from app.models import Item, UserMessage

logger = logging.getLogger(__name__)


@shared_task(name="Cleanup user messages")
def cleanup_user_messages():
    """Delete shown user messages older than the configured retention window."""
    cutoff = timezone.now() - timedelta(days=settings.USER_MESSAGE_RETENTION_DAYS)
    deleted_count, _ = UserMessage.objects.filter(
        shown_at__isnull=False,
        shown_at__lt=cutoff,
    ).delete()

    logger.info("Deleted %s old shown user messages.", deleted_count)

    return deleted_count


@shared_task(name="Download media poster")
def download_item_poster(item_id):
    """Download an item's remote poster, optimize it and cache it locally.

    The image is resized to ``POSTER_MAX_WIDTH`` and re-encoded as WebP to keep
    both storage and outbound bandwidth small. Safe to call repeatedly: it is a
    no-op once a local copy exists or when downloads are disabled.
    """
    if not settings.DOWNLOAD_POSTERS:
        return

    item = Item.objects.filter(pk=item_id).first()
    if (
        item is None
        or item.image_local
        or not item.image
        or item.image == settings.IMG_NONE
    ):
        return

    try:
        response = requests.get(item.image, timeout=settings.REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException:
        logger.warning(
            "Failed to download poster for item %s from %s",
            item_id,
            item.image,
        )
        return

    webp_bytes = helpers.optimize_image(
        response.content,
        settings.POSTER_MAX_WIDTH,
        settings.POSTER_WEBP_QUALITY,
    )
    if webp_bytes is None:
        logger.warning("Invalid image data for item %s", item_id)
        return

    item.image_local.save(
        f"{item_id}.webp",
        ContentFile(webp_bytes),
        save=False,
    )
    item.save(update_fields=["image_local"])
    logger.info("Cached poster for item %s", item_id)
