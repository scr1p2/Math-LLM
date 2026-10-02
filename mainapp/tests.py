from pathlib import Path

from django.test import SimpleTestCase

from utility_scripts.solver import generate_pdf_bytes


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


class PdfGenerationTests(SimpleTestCase):
    def test_generate_pdf_bytes_returns_valid_pdf_payload(self):
        pdf_bytes = generate_pdf_bytes("\\textbf{Test}")

        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertGreater(len(pdf_bytes), 100)
