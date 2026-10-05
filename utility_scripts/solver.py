"""Image transcription, math solving, and LaTeX-to-PDF helpers for the solver."""

import asyncio
import base64
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from ollama import Client

# Models tried in order when an uploaded image must be transcribed.
VISION_MODELS = ["qwen2.5vl"]
# Model used to produce the written, step-by-step math solution.
TEXT_MODEL = "deepseek-r1:latest"
# Bound external LaTeX compilation so a request cannot wait indefinitely.
PDFLATEX_TIMEOUT_SECONDS = 120


@lru_cache(maxsize=1)
def read_prompt() -> str:
    """Read the solver's reusable instructions once per process.

    The prompt is stored outside Python source so solution-writing guidance can
    be updated independently. The one-entry cache avoids disk reads for each
    model request.
    """
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "solver.txt"
    with prompt_path.open("r", encoding="utf-8") as f:
        return f.read()


def ensure_image_bytes(image: Any) -> bytes:
    """Normalize supported image inputs to bytes for the vision model.

    Accepts raw bytes, bytearrays, readable upload/file objects, and file paths.
    Unsupported values fail explicitly rather than being sent to the model in
    an ambiguous format.
    """
    if image is None:
        raise ValueError("image cannot be None")

    if isinstance(image, (bytes, bytearray)):
        return bytes(image)

    if hasattr(image, "read"):
        data = image.read()
        return bytes(data) if isinstance(data, (bytes, bytearray)) else data

    if isinstance(image, str):
        with open(image, "rb") as image_file:
            return image_file.read()

    raise TypeError(f"Unsupported image type: {type(image)!r}")


def _compile_pdf(tex_path: Path) -> bytes:
    """Run pdflatex beside its input file and return the generated PDF bytes.

    Using the temporary directory as the working directory keeps auxiliary
    files next to the source and passing only the filename handles paths with
    spaces consistently. Compiler errors and timeouts are surfaced to callers.
    """
    try:
        completed = subprocess.run(
            [
                "pdflatex",
                # Keep compiler output non-interactive and stop at the first error.
                "-interaction=nonstopmode",
                "-halt-on-error",
                tex_path.name,
            ],
            cwd=tex_path.parent,
            capture_output=True,
            text=True,
            check=False,
            timeout=PDFLATEX_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pdflatex is not installed or not available on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"PDF generation timed out after {PDFLATEX_TIMEOUT_SECONDS} seconds."
        ) from exc

    if completed.returncode != 0:
        # Include both output streams because TeX diagnostics can appear in either.
        error_output = "\n".join(
            stream.strip()
            for stream in (completed.stdout, completed.stderr)
            if stream.strip()
        ) or "Unknown LaTeX error"
        raise RuntimeError(f"PDF generation failed: {error_output}")

    pdf_path = tex_path.with_suffix(".pdf")
    if not pdf_path.exists():
        raise FileNotFoundError(
            "The PDF file was not created in the temporary output directory."
        )
    return pdf_path.read_bytes()


def _solution_document_body(solution_latex: str) -> str:
    """Strip model-added wrappers so the content fits our generated document.

    The solver prompt asks for body-only LaTeX, but this normalization also
    accepts common deviations such as Markdown fences, a document environment,
    a document class, or package imports.
    """
    solution_latex = re.sub(
        r"```(?:latex|tex)?\s*|\s*```",
        "",
        solution_latex,
        flags=re.IGNORECASE,
    )
    document_start = re.search(
        r"\\begin\s*\{\s*document\s*\}",
        solution_latex,
    )
    if document_start:
        # When wrappers are present, keep only the text between them.
        solution_latex = solution_latex[document_start.end():]
        document_end = re.search(
            r"\\end\s*\{\s*document\s*\}",
            solution_latex,
        )
        if document_end:
            solution_latex = solution_latex[:document_end.start()]

    solution_latex = re.sub(
        r"\\(?:documentclass|usepackage)\*?(?:\s*\[[^\]]*\])?\s*\{[^{}]*\}",
        "",
        solution_latex,
    )
    solution_latex = re.sub(
        r"\\(?:begin|end)\s*\{\s*document\s*\}",
        "",
        solution_latex,
    )
    return solution_latex.strip()


def _extract_latex_title(solution_latex: str) -> tuple[str | None, str]:
    """Return the first title and the remaining solution content.

    A character-by-character scan is used instead of a simple brace regex so
    titles containing nested groups remain intact. Backslash-escaped characters
    are skipped while locating the matching closing brace.
    """
    title_command = re.search(r"\\title\s*\{", solution_latex)
    if title_command is None:
        return None, solution_latex

    title_start = title_command.end()
    depth = 1
    index = title_start
    while index < len(solution_latex) and depth:
        if solution_latex[index] == "\\":
            index += 2
            continue
        if solution_latex[index] == "{":
            depth += 1
        elif solution_latex[index] == "}":
            depth -= 1
        index += 1

    if depth:
        raise ValueError("The generated LaTeX title has unbalanced braces.")

    title = solution_latex[title_start:index - 1].strip()
    remaining = solution_latex[:title_command.start()] + solution_latex[index:]
    return title, remaining


def generate_pdf_bytes(solution_latex: str) -> bytes:
    """Wrap a generated LaTeX solution in a complete document and compile it.

    The title is removed from the body and placed in the preamble; if absent or
    empty, a generic title is supplied. The author is explicitly empty because
    solutions do not have an author field. The temporary directory is removed
    automatically after the PDF bytes have been read.
    """
    if not solution_latex:
        raise ValueError("No solution content was provided to generate a PDF.")

    title, solution_without_title = _extract_latex_title(solution_latex)
    title = title or "Step-by-Step Mathematical Solution"
    solution_body = _solution_document_body(solution_without_title)
    if not solution_body:
        raise ValueError("The solution contains no LaTeX document body to render.")

    # Build a controlled preamble so model-generated package/class declarations
    # cannot conflict with the document structure maintained by this module.
    document = "\n".join(
        (
            r"\documentclass{article}",
            r"\usepackage[margin=1in]{geometry}",
            r"\usepackage{amsmath,amssymb}",
            r"\title{" + title + "}",
            r"\author{}",
            r"\begin{document}",
            r"\maketitle",
            solution_body,
            r"\end{document}",
        )
    )

    with tempfile.TemporaryDirectory(prefix="mathllm_") as temp_dir:
        tex_path = Path(temp_dir) / "solution.tex"
        tex_path.write_text(document, encoding="utf-8")
        return _compile_pdf(tex_path)


def process_image(image: Any) -> str:
    """Ask the configured vision model to transcribe an uploaded math problem.

    Ollama's chat API expects image content as base64. Candidate models are
    retried only for errors indicating image-input incompatibility; unrelated
    service/model errors are raised immediately.
    """
    # Convert file or upload input to the base64 image payload Ollama expects.
    img_b64 = base64.b64encode(ensure_image_bytes(image)).decode()
    client = Client()
    last_error = None

    for model_name in VISION_MODELS:
        try:
            resp = client.chat(
                model=model_name,
                messages=[{
                    "options": {
                        "num_ctx": 8000,
                        "num_predict": 4000,
                    },
                    "role": "user",
                    "content": "Transcribe this math problem exactly, using LaTeX for all notation.",
                    "images": [img_b64],
                }],
            )
            return resp["message"]["content"]
        except Exception as exc:
            last_error = exc
            message = str(exc).lower()
            # Only compatibility failures justify trying another vision model.
            if "image input is not supported" in message or "mmproj" in message or "not supported" in message:
                continue
            raise

    raise RuntimeError(
        "No Ollama vision model is available for image input. "
        "Install a multimodal model such as `llava:latest` or `qwen2.5vl:7b`, "
        "then start Ollama again."
    ) from last_error


def solve(problem_text: str) -> str:
    """Generate a detailed LaTeX solution for a transcribed math problem.

    The model receives both the problem and the repository's presentation
    instructions, and is asked to return body content rather than a full TeX
    document; PDF generation owns the document wrapper and compilation.
    """
    client = Client()
    resp = client.chat(
        model=TEXT_MODEL,
        messages=[{
            "role": "user",
            "content": f"Solve step by step, with full justification, in LaTeX. Return only the content that belongs inside a LaTeX document body; do not include a document class, package imports, document environment, or Markdown code fences.\n\n{problem_text}\n\nHere are the instructions I want you to follow:\n\n{read_prompt()}",
        }],
    )
    return resp["message"]["content"]


async def solve_problem_to_pdf(problem_text: str) -> bytes:
    """Run the solve-then-render pipeline without blocking the event loop.

    Both model inference and subprocess-based PDF compilation are synchronous
    operations, so each is moved to a worker thread before awaiting its result.
    """
    solution = await asyncio.to_thread(solve, problem_text)
    return await asyncio.to_thread(generate_pdf_bytes, solution)