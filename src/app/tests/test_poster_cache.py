import tempfile
from io import BytesIO
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase, override_settings
from PIL import Image

from app.models import Item, MediaTypes, Sources
from app.tasks import download_item_poster
from app.templatetags.app_tags import poster_url


def _fake_png_bytes(width=800, height=1200):
    """Return raw PNG bytes for a solid-color image."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class PosterCacheTests(TestCase):
    """Tests for downloading and serving locally cached posters."""

    def setUp(self):
        """Create an item with a remote poster URL."""
        self.item = Item.objects.create(
            media_id="1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Test Game",
            image="http://example.com/image.png",
        )

    def test_image_url_falls_back_to_remote(self):
        """image_url returns the remote URL when nothing is cached."""
        self.assertEqual(self.item.image_url, "http://example.com/image.png")

    def test_poster_url_filter_handles_dict_and_model(self):
        """poster_url works for both provider dicts and Item models."""
        self.assertEqual(
            poster_url({"image": "http://example.com/x.jpg"}),
            "http://example.com/x.jpg",
        )
        self.assertEqual(poster_url(self.item), "http://example.com/image.png")

    @override_settings(DOWNLOAD_POSTERS=True, POSTER_MAX_WIDTH=400)
    @patch("app.tasks.requests.get")
    def test_download_caches_optimized_webp(self, mock_get):
        """The task downloads, resizes and stores the poster as WebP."""
        response = MagicMock()
        response.content = _fake_png_bytes(width=800, height=1200)
        response.raise_for_status = MagicMock()
        mock_get.return_value = response

        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(
                MEDIA_ROOT=media_root,
            ),
        ):
            download_item_poster(self.item.pk)

            self.item.refresh_from_db()
            self.assertTrue(self.item.image_local)
            self.assertTrue(self.item.image_local.name.endswith(".webp"))
            self.assertEqual(self.item.image_url, self.item.image_local.url)

            # Resized down to the configured max width.
            with Image.open(self.item.image_local.path) as cached:
                self.assertEqual(cached.width, 400)
                self.assertEqual(cached.format, "WEBP")

    @override_settings(DOWNLOAD_POSTERS=True)
    @patch("app.tasks.requests.get")
    def test_download_skips_when_already_cached(self, mock_get):
        """A second run is a no-op when a local copy already exists."""
        response = MagicMock()
        response.content = _fake_png_bytes()
        response.raise_for_status = MagicMock()
        mock_get.return_value = response

        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(
                MEDIA_ROOT=media_root,
            ),
        ):
            download_item_poster(self.item.pk)
            self.assertEqual(mock_get.call_count, 1)

            download_item_poster(self.item.pk)
            # Still only called once: the cached copy short-circuits.
            self.assertEqual(mock_get.call_count, 1)

    @override_settings(DOWNLOAD_POSTERS=True)
    @patch("app.tasks.requests.get")
    def test_download_skips_placeholder_image(self, mock_get):
        """The placeholder IMG_NONE is never downloaded."""
        self.item.image = settings.IMG_NONE
        self.item.save(update_fields=["image"])

        download_item_poster(self.item.pk)

        mock_get.assert_not_called()
        self.item.refresh_from_db()
        self.assertFalse(self.item.image_local)
