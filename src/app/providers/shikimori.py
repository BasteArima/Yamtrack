"""Shikimori provider for anime screenshots (fallback gallery).

Shikimori is an unofficial community anime/manga database whose public API
exposes real in-scene screenshots that the official MyAnimeList API does not.
It is used only to enrich anime metadata with a screenshot gallery; images are
hotlinked from the Shikimori CDN, so no bandwidth is spent server-side.

API docs: https://shikimori.one/api/doc
"""

import logging

import requests
from django.core.cache import cache

from app.models import MediaTypes
from app.providers import services

logger = logging.getLogger(__name__)

base_url = "https://shikimori.one"
# Shikimori asks API clients to send a descriptive User-Agent naming the app.
headers = {"User-Agent": "Yamtrack"}

# Max number of gallery images shown on the media details page
GALLERY_LIMIT = 15


def get_gallery(mal_id):
    """Return anime screenshots from Shikimori as a hotlinked gallery.

    Shikimori keys anime by an id that historically matches the MAL id but can
    diverge. To avoid showing screenshots from the wrong title, the anime is
    fetched by ``mal_id`` and the screenshots are only trusted when the returned
    ``myanimelist_id`` matches. On any error or mismatch an empty gallery is
    returned so the section is simply hidden.
    """
    cache_key = f"shikimori_screenshots_{MediaTypes.ANIME.value}_{mal_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    gallery = _fetch_gallery(mal_id)
    cache.set(cache_key, gallery)
    return gallery


def _fetch_gallery(mal_id):
    """Fetch and build the screenshot gallery, returning [] on any failure."""
    try:
        anime = services.api_request(
            "shikimori",
            "GET",
            f"{base_url}/api/animes/{mal_id}",
            headers=headers,
        )
        # Guard against id divergence between Shikimori and MAL
        if str(anime.get("myanimelist_id")) != str(mal_id):
            logger.debug(
                "Shikimori id %s does not map to MAL id %s, skipping gallery",
                anime.get("id"),
                mal_id,
            )
            return []

        screenshots = services.api_request(
            "shikimori",
            "GET",
            f"{base_url}/api/animes/{mal_id}/screenshots",
            headers=headers,
        )
    except (requests.exceptions.RequestException, ValueError):
        logger.warning("Failed to fetch Shikimori screenshots for MAL id %s", mal_id)
        return []

    gallery = []
    for shot in screenshots[:GALLERY_LIMIT]:
        original = shot.get("original")
        preview = shot.get("preview")
        if original and preview:
            gallery.append(
                {
                    "thumb": f"{base_url}{preview}",
                    "full": f"{base_url}{original}",
                },
            )
    return gallery
