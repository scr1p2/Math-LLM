from pathlib import Path

from django.test import SimpleTestCase


class BelowButtonsStyleSyncTests(SimpleTestCase):
    def test_belowbuttons_is_defined_in_shared_stylesheet_only(self):
        project_root = Path(__file__).resolve().parent.parent
        homepage_css = (
            project_root / "mainapp" / "static" / "mainapp" / "homepage.css"
        ).read_text(encoding="utf-8")
        features_css = (
            project_root / "mainapp" / "static" / "mainapp" / "features.css"
        ).read_text(encoding="utf-8")

        self.assertNotIn(".belowbuttons {", homepage_css)
        self.assertIn(".belowbuttons {", features_css)
