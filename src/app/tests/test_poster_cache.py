import tempfile
from io import BytesIO, StringIO
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.management import call_command
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


class DownloadMissingPostersCommandTests(TestCase):
    """Tests for the download_missing_posters backfill command."""

    def setUp(self):
        """Create one item with a real poster URL and one with the placeholder."""
        self.real_item = Item.objects.create(
            media_id="real",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Real",
            image="http://example.com/real.png",
        )
        self.placeholder_item = Item.objects.create(
            media_id="placeholder",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Placeholder",
            image=settings.IMG_NONE,
        )

    @override_settings(DOWNLOAD_POSTERS=True)
    @patch("app.management.commands.download_missing_posters.download_item_poster")
    def test_command_processes_only_real_missing_posters(self, mock_task):
        """--all --sync downloads real uncached posters and skips placeholders."""
        call_command("download_missing_posters", "--all", "--sync", stdout=StringIO())

        called_ids = {call.args[0] for call in mock_task.call_args_list}
        self.assertIn(self.real_item.pk, called_ids)
        self.assertNotIn(self.placeholder_item.pk, called_ids)

    @override_settings(DOWNLOAD_POSTERS=False)
    @patch("app.management.commands.download_missing_posters.download_item_poster")
    def test_command_noop_when_downloads_disabled(self, mock_task):
        """The command does nothing when DOWNLOAD_POSTERS is disabled."""
        stderr = StringIO()
        call_command("download_missing_posters", "--all", stderr=stderr)

        mock_task.assert_not_called()
        self.assertIn("DOWNLOAD_POSTERS is disabled", stderr.getvalue())
