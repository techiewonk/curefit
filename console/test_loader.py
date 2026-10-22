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


class StylesTests(TestCase):
    def test_the_app_styles_carry_a_version_so_a_change_is_fetched_again(self):
        r = self.client.get(reverse("console:revenue"), headers={"Sec-Fetch-Site": "same-origin"})
        self.assertRegex(r.content.decode(), r"console/wide\.css\?v=\d+")
        self.assertRegex(r.content.decode(), r"console/datatable\.js\?v=\d+")

    def test_every_class_in_the_templates_exists_in_a_stylesheet(self):
        """Unfold ships a prebuilt stylesheet: a utility class it lacks does nothing unless wide.css defines it."""
        import re
        from pathlib import Path

        import unfold
        from django.conf import settings
        css = (Path(unfold.__file__).parent / "static/unfold/css/styles.css").read_text(encoding="utf-8")
        css += (settings.BASE_DIR / "console/static/console/wide.css").read_text(encoding="utf-8")
        escape = lambda c: re.sub(r"([:\[\]/.!%#(),])", lambda m: chr(92) + m.group(1), c)  # noqa: E731
        missing = set()
        for page in (settings.BASE_DIR / "console/templates").rglob("*.html"):
            for attr in re.findall(r'class="([^"{}]*)"', page.read_text(encoding="utf-8")):
                for name in attr.split():
                    forms = {"." + escape(name), "." + name}
                    if name[0].isdigit():  # CSS writes a leading digit as an escape: 2xl -> xl
                        forms.add("." + chr(92) + "3" + name[0] + escape(name[1:]))
                    if not any(f in css for f in forms):
                        missing.add(f"{page.name}: {name}")
        self.assertEqual(sorted(missing), [])
