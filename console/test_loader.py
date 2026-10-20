from django.test import TestCase
from django.urls import reverse


class LoaderTests(TestCase):
    """Every page carries the loader: a bar, an overlay after a moment, a busy button (console/loader.js)."""

    def test_every_kind_of_page_loads_the_loader(self):
        for url in ("/admin/", reverse("console:run"), reverse("console:revenue"), reverse("console:perf_analysis"),
                    reverse("console:coverage"), reverse("admin:scraper_session_changelist"),
                    reverse("admin:scraper_centermonth_changelist"), reverse("admin:centers_center_changelist")):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200, url)
            self.assertContains(page, "console/loader.js", msg_prefix=url)
            self.assertContains(page, "console/wide.css", msg_prefix=url)  # the loader's styles

    def test_the_script_and_styles_are_shipped(self):
        from django.contrib.staticfiles import finders
        script = open(finders.find("console/loader.js"), encoding="utf-8").read()
        styles = open(finders.find("console/wide.css"), encoding="utf-8").read()
        for needle in ("htmx:beforeRequest", "pageshow", "OVERLAY_AFTER", "cf-busy"):
            self.assertIn(needle, script)
        for needle in (".cf-loader-bar", ".cf-loader-overlay", "prefers-reduced-motion"):
            self.assertIn(needle, styles)
