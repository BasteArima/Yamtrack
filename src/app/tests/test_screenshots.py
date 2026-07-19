import tempfile
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from app import helpers
from app.models import Item, ItemScreenshot, MediaTypes, Movie, Sources, Status
from app.providers import manual


def _fake_png_bytes(width=800, height=600):
    """Return raw PNG bytes for a solid-color image."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class OptimizeImageTests(TestCase):
    """Tests for the shared resize/WebP helper."""

    def test_reencodes_to_webp(self):
        """Valid image bytes are returned re-encoded as WebP."""
        webp = helpers.optimize_image(_fake_png_bytes(), max_width=1280, quality=80)

        self.assertIsNotNone(webp)
        self.assertEqual(Image.open(BytesIO(webp)).format, "WEBP")

    def test_resizes_when_wider_than_max(self):
        """An image wider than max_width is scaled down, preserving ratio."""
        webp = helpers.optimize_image(
            _fake_png_bytes(width=2000, height=1000),
            max_width=1000,
            quality=80,
        )
        image = Image.open(BytesIO(webp))

        self.assertEqual(image.width, 1000)
        self.assertEqual(image.height, 500)

    def test_invalid_bytes_returns_none(self):
        """Non-image bytes yield None instead of raising."""
        self.assertIsNone(
            helpers.optimize_image(b"not an image", max_width=1280, quality=80),
        )


class ItemScreenshotModelTests(TestCase):
    """Tests for the ItemScreenshot model."""

    def setUp(self):
        """Create a manual item to attach screenshots to."""
        self.item = Item.objects.create(
            media_id="custom-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Custom Movie",
            image="http://example.com/poster.jpg",
        )

    def test_src_prefers_url_when_no_file(self):
        """The src property returns the external URL for url-only screenshots."""
        shot = ItemScreenshot.objects.create(
            item=self.item,
            url="http://example.com/shot.jpg",
        )
        self.assertEqual(shot.src, "http://example.com/shot.jpg")

    def test_screenshots_ordered_by_position(self):
        """Screenshots come back ordered by position."""
        ItemScreenshot.objects.create(item=self.item, url="b", position=1)
        ItemScreenshot.objects.create(item=self.item, url="a", position=0)

        urls = [s.url for s in self.item.screenshots.all()]
        self.assertEqual(urls, ["a", "b"])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ManualGalleryTests(TestCase):
    """Tests for exposing custom-item screenshots as a gallery."""

    def test_manual_metadata_includes_gallery(self):
        """manual.metadata surfaces stored screenshots as a gallery, in order."""
        item = Item.objects.create(
            media_id="custom-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Custom Game",
            image="http://example.com/poster.jpg",
        )
        ItemScreenshot.objects.create(item=item, url="http://x/2.jpg", position=1)
        ItemScreenshot.objects.create(item=item, url="http://x/1.jpg", position=0)

        metadata = manual.metadata("custom-2", MediaTypes.GAME.value)

        self.assertEqual(
            metadata["gallery"],
            [
                {"thumb": "http://x/1.jpg", "full": "http://x/1.jpg"},
                {"thumb": "http://x/2.jpg", "full": "http://x/2.jpg"},
            ],
        )

    def test_manual_metadata_empty_gallery(self):
        """An item with no screenshots yields an empty gallery."""
        Item.objects.create(
            media_id="custom-3",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="No Shots",
            image="http://example.com/poster.jpg",
        )
        metadata = manual.metadata("custom-3", MediaTypes.MOVIE.value)
        self.assertEqual(metadata["gallery"], [])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class AddScreenshotsTests(TestCase):
    """Tests for helpers.add_screenshots (files + URLs)."""

    def setUp(self):
        """Create a manual item to attach screenshots to."""
        self.item = Item.objects.create(
            media_id="custom-add",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Custom",
            image="http://example.com/poster.jpg",
        )

    def test_adds_files_then_urls_skipping_invalid(self):
        """Valid files and URLs are stored in order; blanks/invalid are skipped."""
        files = [SimpleUploadedFile("a.png", _fake_png_bytes(), "image/png")]
        urls = [
            "https://example.com/1.jpg",
            "  ",
            "not a url",
            "https://example.com/2.jpg",
        ]

        created = helpers.add_screenshots(self.item, files, urls)

        self.assertEqual(created, 3)
        shots = list(self.item.screenshots.all())
        self.assertTrue(shots[0].image.name.endswith(".webp"))
        self.assertEqual(shots[0].position, 0)
        self.assertEqual(
            [shots[1].url, shots[2].url],
            ["https://example.com/1.jpg", "https://example.com/2.jpg"],
        )

    def test_invalid_image_is_skipped(self):
        """A non-image upload is skipped rather than stored."""
        files = [SimpleUploadedFile("bad.png", b"nope", "image/png")]
        self.assertEqual(helpers.add_screenshots(self.item, files, []), 0)

    def test_respects_cap(self):
        """No more than MAX_SCREENSHOTS_PER_ITEM screenshots are created."""
        urls = [
            f"https://example.com/{i}.jpg"
            for i in range(helpers.MAX_SCREENSHOTS_PER_ITEM + 5)
        ]

        created = helpers.add_screenshots(self.item, [], urls)

        self.assertEqual(created, helpers.MAX_SCREENSHOTS_PER_ITEM)
        self.assertEqual(
            self.item.screenshots.count(),
            helpers.MAX_SCREENSHOTS_PER_ITEM,
        )


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ScreenshotEndpointTests(TestCase):
    """Tests for the manage-screenshots endpoints (ownership, add, delete, reorder)."""

    def setUp(self):
        """Create a logged-in user who owns a custom movie."""
        self.credentials = {"username": "owner", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="custom-ep",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Owned Custom",
            image="http://example.com/poster.jpg",
        )
        Movie.objects.create(
            item=self.item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

    def _kwargs(self):
        return {
            "source": Sources.MANUAL.value,
            "media_type": MediaTypes.MOVIE.value,
            "media_id": self.item.media_id,
        }

    def test_modal_renders_for_owner(self):
        """The owner gets the manage-screenshots modal."""
        response = self.client.get(reverse("screenshots_modal", kwargs=self._kwargs()))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_screenshots.html")

    def test_modal_404_for_non_owner(self):
        """A user who doesn't own the item cannot open the modal."""
        other = {"username": "intruder", "password": "12345"}
        get_user_model().objects.create_user(**other)
        self.client.login(**other)

        response = self.client.get(reverse("screenshots_modal", kwargs=self._kwargs()))
        self.assertEqual(response.status_code, 404)

    def test_modal_404_for_non_manual_source(self):
        """Screenshots can only be managed on manual items."""
        response = self.client.get(
            reverse(
                "screenshots_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                },
            ),
        )
        self.assertEqual(response.status_code, 404)

    def test_add_urls(self):
        """Posting URLs creates screenshots on the item."""
        response = self.client.post(
            reverse("screenshots_add", kwargs=self._kwargs()),
            {"screenshot_urls": "https://example.com/1.jpg\nhttps://example.com/2.jpg"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.item.screenshots.count(), 2)

    def test_delete(self):
        """Deleting a screenshot removes it."""
        shot = ItemScreenshot.objects.create(
            item=self.item,
            url="https://example.com/1.jpg",
        )
        response = self.client.post(
            reverse("screenshots_delete", kwargs={"screenshot_id": shot.id}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.item.screenshots.exists())

    def test_media_details_shows_manage_button_and_gallery(self):
        """The custom item's details page shows the manage button and gallery."""
        ItemScreenshot.objects.create(
            item=self.item,
            url="https://example.com/shot.jpg",
        )
        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MANUAL.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": self.item.media_id,
                    "title": "owned-custom",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manage screenshots")
        self.assertContains(response, "https://example.com/shot.jpg")

    def test_reorder(self):
        """Posting a new order persists screenshot positions."""
        first = ItemScreenshot.objects.create(item=self.item, url="a", position=0)
        second = ItemScreenshot.objects.create(item=self.item, url="b", position=1)

        response = self.client.post(
            reverse("screenshots_reorder", kwargs=self._kwargs()),
            {"order": [second.id, first.id]},
        )

        self.assertEqual(response.status_code, 204)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(second.position, 0)
        self.assertEqual(first.position, 1)
