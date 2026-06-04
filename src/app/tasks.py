import logging
from datetime import timedelta
from io import BytesIO

import requests
from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image

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

    try:
        image = Image.open(BytesIO(response.content))
        image.load()
    except OSError:
        logger.warning("Invalid image data for item %s", item_id)
        return

    if image.mode != "RGB":
        image = image.convert("RGB")

    max_width = settings.POSTER_MAX_WIDTH
    if max_width and image.width > max_width:
        ratio = max_width / image.width
        new_height = max(1, round(image.height * ratio))
        image = image.resize((max_width, new_height), Image.LANCZOS)

    buffer = BytesIO()
    image.save(buffer, format="WEBP", quality=settings.POSTER_WEBP_QUALITY)

    item.image_local.save(
        f"{item_id}.webp",
        ContentFile(buffer.getvalue()),
        save=False,
    )
    item.save(update_fields=["image_local"])
    logger.info("Cached poster for item %s", item_id)
