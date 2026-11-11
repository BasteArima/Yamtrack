from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import TestCase

from app import views
from app.models import MediaTypes, Sources
from app.providers import mal

GALLERY = [{"thumb": "t", "full": "f"}]


class FallbackGalleryTests(TestCase):
    """The MAL-keyed fallback gallery picks the MAL id by source."""

    @patch("app.views.shikimori.get_gallery", return_value=GALLERY)
    def test_mal_anime_uses_its_own_id(self, mock_shikimori):
        """MAL anime query Shikimori with their own id."""
        metadata = {}
        views._add_fallback_gallery(
            metadata,
            Sources.MAL.value,
            MediaTypes.ANIME.value,
            "1",
        )

        mock_shikimori.assert_called_once_with("1")
        self.assertEqual(metadata["gallery"], GALLERY)

    @patch("app.views.shikimori.get_gallery", return_value=GALLERY)
    def test_anilist_anime_uses_id_mal(self, mock_shikimori):
        """AniList anime query Shikimori with idMal, never the AniList id."""
        metadata = {"mal_id": 5114}
        views._add_fallback_gallery(
            metadata,
            Sources.ANILIST.value,
            MediaTypes.ANIME.value,
            "5114000",
        )

        mock_shikimori.assert_called_once_with(5114)
        self.assertEqual(metadata["gallery"], GALLERY)

    @patch("app.views.shikimori.get_gallery")
    def test_anilist_anime_without_id_mal(self, mock_shikimori):
        """AniList anime without a MAL counterpart get no fallback gallery."""
        metadata = {"mal_id": None}
        views._add_fallback_gallery(
            metadata,
            Sources.ANILIST.value,
            MediaTypes.ANIME.value,
            "1",
        )

        mock_shikimori.assert_not_called()
        self.assertNotIn("gallery", metadata)

    @patch("app.views.shikimori.get_gallery")
    def test_manual_anime_keeps_own_gallery(self, mock_shikimori):
        """Custom items keep their uploaded screenshots."""
        metadata = {"gallery": GALLERY}
        views._add_fallback_gallery(
            metadata,
            Sources.MANUAL.value,
            MediaTypes.ANIME.value,
            "1",
        )

        mock_shikimori.assert_not_called()
        self.assertEqual(metadata["gallery"], GALLERY)

    @patch("app.views.shikimori.get_gallery")
    def test_manual_anime_without_gallery(self, mock_shikimori):
        """Custom items have no MAL id, so no fallback is fetched."""
        metadata = {"gallery": []}
        views._add_fallback_gallery(
            metadata,
            Sources.MANUAL.value,
            MediaTypes.ANIME.value,
            "1",
        )

        mock_shikimori.assert_not_called()

    @patch("app.views.mal.get_manga_gallery", return_value=GALLERY)
    def test_anilist_manga_uses_mal_pictures(self, mock_mal):
        """AniList manga get MAL alternate covers through idMal."""
        metadata = {"mal_id": 2}
        views._add_fallback_gallery(
            metadata,
            Sources.ANILIST.value,
            MediaTypes.MANGA.value,
            "30002",
        )

        mock_mal.assert_called_once_with(2)
        self.assertEqual(metadata["gallery"], GALLERY)


class MalMangaGalleryTests(TestCase):
    """mal.get_manga_gallery fetches only the pictures and never raises."""

    def setUp(self):
        """Start from an empty cache."""
        cache.clear()

    @patch("app.providers.mal.services.api_request")
    def test_pictures(self, mock_api):
        """Pictures become a gallery."""
        mock_api.return_value = {"pictures": [{"medium": "m", "large": "l"}]}

        self.assertEqual(mal.get_manga_gallery(2), [{"thumb": "m", "full": "l"}])
        self.assertEqual(mock_api.call_args.kwargs["params"], {"fields": "pictures"})

    @patch("app.providers.mal.services.api_request")
    def test_error_returns_empty(self, mock_api):
        """A failing request results in an empty gallery."""
        mock_api.side_effect = requests.exceptions.ConnectionError()

        self.assertEqual(mal.get_manga_gallery(3), [])
