import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import AsyncRequestFactory, SimpleTestCase

from mainapp.views import solver as solver_view
from utility_scripts.solver import (
    PDFLATEX_TIMEOUT_SECONDS,
    _compile_pdf,
    generate_pdf_bytes,
    solve_problem_to_pdf,
)


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

    def test_global_scrollbar_styles_are_defined_in_shared_stylesheet(self):
        project_root = Path(__file__).resolve().parent.parent
        features_css = (
            project_root / "mainapp" / "static" / "mainapp" / "features.css"
        ).read_text(encoding="utf-8")

        self.assertIn("scrollbar-color: #8ec7c0 #252d38;", features_css)
        self.assertIn("scrollbar-width: auto;", features_css)
        self.assertIn("*::-webkit-scrollbar-thumb", features_css)


class PdfGenerationTests(SimpleTestCase):
    def test_generate_pdf_bytes_returns_valid_pdf_payload(self):
        pdf_bytes = generate_pdf_bytes("\\textbf{Test}")

        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertGreater(len(pdf_bytes), 100)

    def test_generate_pdf_bytes_removes_generated_document_wrappers(self):
        solution = (
            r"\title{Proof of \(\sqrt{2}\) Irrationality}"
            r"\usepackage{amsmath,amssymb}\begin{document}"
            r"\documentclass{article}We will prove the result step by step."
            r"\end{document}"
        )
        pdf_bytes = generate_pdf_bytes(solution)

        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_generate_pdf_bytes_includes_a_fallback_title(self):
        pdf_bytes = generate_pdf_bytes(r"\textbf{Test}")

        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertGreater(len(pdf_bytes), 100)

    def test_generate_pdf_bytes_rejects_empty_normalized_body(self):
        with self.assertRaisesRegex(
            ValueError,
            "no LaTeX document body",
        ):
            generate_pdf_bytes(
                r"\title{Empty}\begin{document}\end{document}"
            )

    def test_compile_pdf_uses_filename_from_temporary_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_dir = Path(temp_dir) / "directory with spaces"
            tex_dir.mkdir()
            tex_path = tex_dir / "solution.tex"
            tex_path.write_text("\\documentclass{article}", encoding="utf-8")
            pdf_bytes = b"%PDF-1.4 test"

            def create_pdf(*args, **kwargs):
                (Path(kwargs["cwd"]) / "solution.pdf").write_bytes(pdf_bytes)
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with patch(
                "utility_scripts.solver.subprocess.run",
                side_effect=create_pdf,
            ) as run:
                self.assertEqual(_compile_pdf(tex_path), pdf_bytes)

        command = run.call_args.args[0]
        self.assertEqual(command[-1], "solution.tex")
        self.assertEqual(run.call_args.kwargs["cwd"], tex_dir)
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            PDFLATEX_TIMEOUT_SECONDS,
        )

    def test_compile_pdf_reports_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_path = Path(temp_dir) / "solution.tex"
            tex_path.write_text("\\documentclass{article}", encoding="utf-8")

            with patch(
                "utility_scripts.solver.subprocess.run",
                side_effect=subprocess.TimeoutExpired("pdflatex", 120),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    f"timed out after {PDFLATEX_TIMEOUT_SECONDS} seconds",
                ):
                    _compile_pdf(tex_path)


class SolverPipelineTests(SimpleTestCase):
    def test_solve_problem_to_pdf_runs_stages_in_order(self):
        with (
            patch("utility_scripts.solver.solve", return_value="solution") as solve,
            patch(
                "utility_scripts.solver.generate_pdf_bytes",
                return_value=b"%PDF",
            ) as generate_pdf,
        ):
            result = asyncio.run(solve_problem_to_pdf("problem"))

        self.assertEqual(result, b"%PDF")
        solve.assert_called_once_with("problem")
        generate_pdf.assert_called_once_with("solution")


class SolverViewTests(SimpleTestCase):
    def setUp(self):
        self.request_factory = AsyncRequestFactory()

    def test_solve_action_returns_solution_before_pdf_generation(self):
        request = self.request_factory.post(
            "/solver/",
            {"action": "solve", "problem": "problem"},
        )

        with patch("mainapp.views.solve", return_value="solution") as solve:
            response = asyncio.run(solver_view(request))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.content),
            {"solution": "solution"},
        )
        solve.assert_called_once_with("problem")

    def test_pdf_action_returns_generated_pdf(self):
        request = self.request_factory.post(
            "/solver/",
            {"action": "pdf", "solution": "solution"},
        )

        with patch(
            "mainapp.views.generate_pdf_bytes",
            return_value=b"%PDF",
        ) as generate_pdf:
            response = asyncio.run(solver_view(request))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response.content, b"%PDF")
        generate_pdf.assert_called_once_with("solution")
