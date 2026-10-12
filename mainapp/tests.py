import asyncio
import io
import json
import subprocess
import tempfile
import time
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from PIL import Image

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import AsyncRequestFactory, RequestFactory, SimpleTestCase

from mainapp.views import solver as solver_view
from utility_scripts.solver import (
    MAX_IMAGE_BYTES,
    PDFLATEX_TIMEOUT_SECONDS,
    _compile_pdf,
    _escape_latex_text_specials,
    _resize_image_bytes,
    generate_pdf_bytes,
    process_image,
    process_image_events,
    solve,
    solve_events,
    solve_problem_to_pdf,
)


async def collect_streaming_response(response):
    return b"".join([chunk async for chunk in response.streaming_content]).decode()


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

    def test_theme_control_styles_are_defined_in_shared_stylesheet(self):
        project_root = Path(__file__).resolve().parent.parent
        features_css = (
            project_root / "mainapp" / "static" / "mainapp" / "features.css"
        ).read_text(encoding="utf-8")
        pages_css = (
            project_root / "mainapp" / "static" / "mainapp" / "pages.css"
        ).read_text(encoding="utf-8")

        self.assertIn(".theme-toggle {", features_css)
        self.assertNotIn(".theme-toggle {", pages_css)


class SharedTemplateTests(SimpleTestCase):
    def test_pages_render_shared_theme_control_and_page_assets(self):
        pages = (
            ("/", "MathLLM Homepage", "homepage.css", None),
            ("/about/", "About MathLLM", "about.css", None),
            ("/llms/", "LLMs to Use", "llms.css", None),
            ("/generator/", "Problem Generator", "pages.css", None),
            ("/solver/", "Solver", "pages.css", "js/solver.js"),
        )

        for path, title, stylesheet, script in pages:
            with self.subTest(path=path):
                response = self.client.get(path)

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f"<title>{title}</title>")
                self.assertContains(response, 'id="theme-toggle"')
                self.assertContains(response, f"mainapp/{stylesheet}")
                if script:
                    self.assertContains(response, f"mainapp/{script}")


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

    def test_generate_pdf_bytes_supports_solutions_without_an_author(self):
        generated_tex = []

        def capture_generated_tex(tex_path):
            generated_tex.append(tex_path.read_text(encoding="utf-8"))
            return b"%PDF"

        with patch(
            "utility_scripts.solver._compile_pdf",
            side_effect=capture_generated_tex,
        ):
            generate_pdf_bytes(r"\textbf{Test}")

        self.assertIn(r"\author{}", generated_tex[0])
        self.assertIn(r"\pdfcompresslevel=9", generated_tex[0])
        self.assertIn(r"\pdfobjcompresslevel=3", generated_tex[0])

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

    def test_compile_pdf_reports_compact_latex_diagnostic_and_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_path = Path(temp_dir) / "solution.tex"
            tex_path.write_text("\\documentclass{article}", encoding="utf-8")

            with (
                patch(
                    "utility_scripts.solver.subprocess.run",
                    return_value=SimpleNamespace(
                        returncode=1,
                        stdout="! Missing $ inserted.\n<inserted text>\nl.17",
                        stderr="Transcript written on solution.log.",
                    ),
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    r"line 17: ! Missing \$ inserted",
                ),
            ):
                _compile_pdf(tex_path)

    def test_escape_latex_text_specials_preserves_math_and_escapes_prose(self):
        source = r"Variable_name is 40% of A&B; \(x_1\) and $y_2$ stay math."

        self.assertEqual(
            _escape_latex_text_specials(source),
            r"Variable\_name is 40\% of A\&B; \(x_1\) and $y_2$ stay math.",
        )

    def test_generate_pdf_bytes_escapes_specials_in_text_but_not_math(self):
        generated_tex = []

        def capture_generated_tex(tex_path):
            generated_tex.append(tex_path.read_text(encoding="utf-8"))
            return b"%PDF"

        with patch(
            "utility_scripts.solver._compile_pdf",
            side_effect=capture_generated_tex,
        ):
            generate_pdf_bytes(r"The value is 50% and file_name; \(x_1\).")

        self.assertIn(r"The value is 50\% and file\_name; \(x_1\).", generated_tex[0])


class SolverPipelineTests(SimpleTestCase):
    def test_solve_problem_to_pdf_runs_stages_in_order(self):
        with (
            patch(
                "utility_scripts.solver.solve",
                return_value=iter(["solu", "tion"]),
            ) as solve_mock,
            patch(
                "utility_scripts.solver.generate_pdf_bytes",
                return_value=b"%PDF",
            ) as generate_pdf,
        ):
            result = asyncio.run(solve_problem_to_pdf("problem"))

        self.assertEqual(result, b"%PDF")
        solve_mock.assert_called_once_with("problem")
        generate_pdf.assert_called_once_with("solution")

    def test_solve_streams_the_solution_from_one_ollama_request(self):
        chat = Mock(return_value=iter([
            SimpleNamespace(message=SimpleNamespace(content="Step 1")),
            SimpleNamespace(message=SimpleNamespace(content=": result")),
        ]))
        client = SimpleNamespace(
            chat=chat,
        )

        with patch("utility_scripts.solver.Client", return_value=client):
            chunks = list(solve("problem"))

        self.assertEqual(chunks, ["Step 1", ": result"])
        chat.assert_called_once()
        self.assertTrue(chat.call_args.kwargs["stream"])

    def test_solve_reports_when_ollama_cannot_be_reached(self):
        with (
            patch(
                "utility_scripts.solver.Client",
                return_value=SimpleNamespace(
                    chat=Mock(side_effect=httpx.ConnectError("connection refused"))
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "Could not connect to Ollama"),
        ):
            list(solve("problem"))

    def test_process_image_streams_transcription_from_one_ollama_request(self):
        chat = Mock(return_value=iter([
            SimpleNamespace(message=SimpleNamespace(content="Problem ")),
            SimpleNamespace(message=SimpleNamespace(content="text")),
        ]))
        client = SimpleNamespace(chat=chat)

        with (
            patch("utility_scripts.solver._image_to_ollama_payload", return_value="image"),
            patch("utility_scripts.solver.Client", return_value=client),
        ):
            chunks = list(process_image(b"image bytes"))

        self.assertEqual(chunks, ["Problem ", "text"])
        chat.assert_called_once()
        self.assertTrue(chat.call_args.kwargs["stream"])

    def test_process_image_reports_when_ollama_cannot_be_reached(self):
        client = SimpleNamespace(
            chat=Mock(side_effect=httpx.ConnectError("connection refused"))
        )
        with (
            patch("utility_scripts.solver._image_to_ollama_payload", return_value="image"),
            patch("utility_scripts.solver.Client", return_value=client),
            self.assertRaisesRegex(RuntimeError, "Could not connect to Ollama"),
        ):
            list(process_image_events(b"image bytes"))

    def test_process_image_events_report_model_progress_and_transcription(self):
        chat = Mock(side_effect=[
            RuntimeError("image input is not supported"),
            iter([SimpleNamespace(message=SimpleNamespace(content="Problem"))]),
        ])
        client = SimpleNamespace(chat=chat)

        with (
            patch("utility_scripts.solver._image_to_ollama_payload", return_value="image"),
            patch("utility_scripts.solver.Client", return_value=client),
            patch("utility_scripts.solver.VISION_MODELS", ["unsupported", "working"]),
        ):
            events = list(process_image_events(b"image bytes"))

        self.assertEqual(
            events,
            [
                ("status", "Preparing the uploaded image for the vision model..."),
                ("status", "Transcribing with unsupported (1/2)..."),
                (
                    "status",
                    "unsupported does not support image input; trying the next model.",
                ),
                ("status", "Transcribing with working (2/2)..."),
                ("content", "Problem"),
            ],
        )

    def test_solve_events_only_stream_user_facing_solution(self):
        with patch(
            "utility_scripts.solver.solve",
            return_value=iter(["Step 1", ": result"]),
        ):
            events = list(solve_events("problem"))

        self.assertEqual(
            events,
            [
                ("status", "Starting deepseek-r1:latest..."),
                ("content", "Step 1"),
                ("content", ": result"),
            ],
        )

    def test_resize_image_bytes_compresses_large_uploads(self):
        image = Image.effect_noise((5000, 5000), 256)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        resized = _resize_image_bytes(buffer.getvalue())

        self.assertLess(len(resized), len(buffer.getvalue()))
        self.assertLess(len(resized), MAX_IMAGE_BYTES)


class SolverViewTests(SimpleTestCase):
    def setUp(self):
        self.request_factory = AsyncRequestFactory()

    def test_solve_action_streams_the_generated_solution(self):
        request = self.request_factory.post(
            "/solver/",
            {"action": "solve", "problem": "problem"},
        )

        consumed_events = []

        def stream_events():
            consumed_events.append("status")
            yield ("status", "Starting DeepSeek")
            consumed_events.append("content")
            yield ("content", "Step 1")

        with patch(
            "mainapp.views.solve_events",
            return_value=stream_events(),
        ) as solve_mock:
            response = asyncio.run(solver_view(request))
            self.assertTrue(response.is_async)

            async def consume_incrementally():
                stream = response.streaming_content.__aiter__()
                first_chunk = await anext(stream)
                state_after_first_chunk = list(consumed_events)
                remainder = b"".join([chunk async for chunk in stream])
                return first_chunk + remainder, state_after_first_chunk

            response_bytes, consumed_after_first_chunk = asyncio.run(consume_incrementally())
            response_text = response_bytes.decode()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        self.assertEqual(consumed_after_first_chunk, ["status"])
        self.assertIn('"type": "status", "text": "Starting DeepSeek"', response_text)
        self.assertIn('"text": "Step 1"', response_text)
        self.assertIn('"type": "done"', response_text)
        solve_mock.assert_called_once_with("problem")

    def test_read_action_streams_image_transcription(self):
        uploaded_file = SimpleUploadedFile("problem.png", b"image bytes")
        request = self.request_factory.post(
            "/solver/",
            {"action": "read", "fileInput": uploaded_file},
        )

        with patch(
            "mainapp.views.process_image_events",
            return_value=iter([
                ("status", "Preparing image"),
                ("content", "Problem "),
                ("content", "text"),
            ]),
        ) as process_image_mock:
            response = asyncio.run(solver_view(request))
            response_text = asyncio.run(collect_streaming_response(response))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        self.assertIn('"type": "status", "text": "Preparing image"', response_text)
        self.assertIn('"text": "Problem "', response_text)
        self.assertIn('"text": "text"', response_text)
        self.assertIn('"type": "done"', response_text)
        process_image_mock.assert_called_once()
        self.assertEqual(process_image_mock.call_args.args[0].name, "problem.png")

    def test_read_action_stream_surfaces_ollama_connection_error(self):
        request = self.request_factory.post(
            "/solver/",
            {
                "action": "read",
                "fileInput": SimpleUploadedFile("problem.png", b"image bytes"),
            },
        )

        def failing_events():
            yield ("status", "Transcribing...")
            raise RuntimeError("Could not connect to Ollama. Start the Ollama app.")

        with patch(
            "mainapp.views.process_image_events",
            return_value=failing_events(),
        ):
            response = asyncio.run(solver_view(request))
            response_text = asyncio.run(collect_streaming_response(response))

        self.assertIn('"type": "error"', response_text)
        self.assertIn("Could not connect to Ollama", response_text)

    def test_solver_stream_reports_progress_while_model_is_waiting(self):
        request = self.request_factory.post(
            "/solver/",
            {"action": "solve", "problem": "problem"},
        )

        def delayed_events():
            yield ("status", "Starting DeepSeek")
            time.sleep(0.04)
            yield ("content", "Answer")

        with (
            patch("mainapp.views.solve_events", return_value=delayed_events()),
            patch("mainapp.views.STREAM_PROGRESS_INTERVAL_SECONDS", 0.01),
        ):
            response = asyncio.run(solver_view(request))
            response_text = asyncio.run(collect_streaming_response(response))

        self.assertIn('"type": "progress"', response_text)
        self.assertIn("deepseek-r1:latest is reasoning", response_text)
        self.assertIn('"text": "Answer"', response_text)

    def test_wsgi_solver_stream_uses_sync_iterator_without_adaptation_warning(self):
        request = RequestFactory().post(
            "/solver/",
            {"action": "solve", "problem": "problem"},
        )

        with patch(
            "mainapp.views.solve_events",
            return_value=iter([("content", "Answer")]),
        ):
            response = asyncio.run(solver_view(request))
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                response_text = b"".join(response.streaming_content).decode()

        self.assertFalse(response.is_async)
        self.assertFalse(any("StreamingHttpResponse must consume asynchronous iterators" in str(item.message) for item in caught))
        self.assertIn('"text": "Answer"', response_text)
        self.assertIn('"type": "done"', response_text)

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

    def test_pdf_action_returns_actionable_latex_error(self):
        request = self.request_factory.post(
            "/solver/",
            {"action": "pdf", "solution": "malformed solution"},
        )

        with patch(
            "mainapp.views.generate_pdf_bytes",
            side_effect=RuntimeError(
                "PDF generation failed at generated LaTeX line 17: ! Missing $ inserted."
            ),
        ):
            response = asyncio.run(solver_view(request))

        error = json.loads(response.content)["error"]
        self.assertEqual(response.status_code, 422)
        self.assertIn("line 17", error)
        self.assertIn("Missing $ inserted", error)
