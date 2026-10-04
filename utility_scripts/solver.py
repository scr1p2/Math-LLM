import asyncio
import base64
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from ollama import Client

# Supported multimodal models for extracting text from uploaded images.
VISION_MODELS = ["qwen2.5vl"]
# Default language model used to solve the transcribed math problem.
TEXT_MODEL = "deepseek-r1:latest"
PDFLATEX_TIMEOUT_SECONDS = 120


@lru_cache(maxsize=1)
def read_prompt() -> str:
    """Load and cache the system prompt used to instruct the solver model."""
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "solver.txt"
    with prompt_path.open("r", encoding="utf-8") as f:
        return f.read()


def ensure_image_bytes(image: Any) -> bytes:
    """Return raw bytes from a file path, file-like object, or raw byte payload."""
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
    """Compile a TeX file from its containing directory and return the PDF bytes."""
    try:
        completed = subprocess.run(
            [
                "pdflatex",
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
    """Remove document wrappers and preamble commands from generated LaTeX."""
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
    """Extract the first balanced LaTeX title command from generated content."""
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
    """Compile a LaTeX solution in an isolated temporary directory and return its PDF bytes."""
    if not solution_latex:
        raise ValueError("No solution content was provided to generate a PDF.")

    title, solution_without_title = _extract_latex_title(solution_latex)
    title = title or "Step-by-Step Mathematical Solution"
    solution_body = _solution_document_body(solution_without_title)
    if not solution_body:
        raise ValueError("The solution contains no LaTeX document body to render.")

    document = "\n".join(
        (
            r"\documentclass{article}",
            r"\usepackage[margin=1in]{geometry}",
            r"\usepackage{amsmath,amssymb}",
            r"\title{" + title + "}",
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
    """Extract the problem statement from an uploaded image using the vision-capable model."""
    # Convert the uploaded image into base64 so Ollama can accept it as a multimodal payload.
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
            # Skip models that do not support image input and continue to the next candidate.
            if "image input is not supported" in message or "mmproj" in message or "not supported" in message:
                continue
            raise

    raise RuntimeError(
        "No Ollama vision model is available for image input. "
        "Install a multimodal model such as `llava:latest` or `qwen2.5vl:7b`, "
        "then start Ollama again."
    ) from last_error


def solve(problem_text: str) -> str:
    """Use the text model to generate a LaTeX-formatted step-by-step solution."""
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
    """Generate a PDF solution without blocking the async caller."""
    solution = await asyncio.to_thread(solve, problem_text)
    return await asyncio.to_thread(generate_pdf_bytes, solution)