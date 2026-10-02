import asyncio
import base64
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ollama import Client

# Supported multimodal models for extracting text from uploaded images.
VISION_MODELS = ["qwen2.5vl"]
# Default language model used to solve the transcribed math problem.
TEXT_MODEL = "deepseek-r1:latest"


def read_prompt() -> str:
    """Load the system prompt used to instruct the solver model."""
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


def generate_pdf_bytes(solution_latex: str) -> bytes:
    """Compile a LaTeX solution into a PDF in a temporary directory and return the bytes.

    This avoids relying on a repository-relative output path while still producing a valid
    PDF payload for the browser or any other caller.
    """
    if not solution_latex:
        raise ValueError("No solution content was provided to generate a PDF.")

    # Build a minimal LaTeX document around the generated solution text.
    document = (
        r"\documentclass{article}"
        r"\usepackage[margin=1in]{geometry}"
        r"\usepackage{amsmath,amssymb}"
        r"\begin{document}"
        + solution_latex
        + r"\end{document}"
    )

    with tempfile.TemporaryDirectory(prefix="mathllm_") as temp_dir:
        temp_path = Path(temp_dir)
        tex_path = temp_path / "solution.tex"
        tex_path.write_text(document, encoding="utf-8")

        try:
            # Compile the TeX document into a PDF using the system-installed pdflatex binary.
            completed = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", str(tex_path)],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("pdflatex is not installed or not available on PATH.") from exc

        # Stop immediately if LaTeX reports an error so the caller sees the actual failure.
        if completed.returncode != 0:
            error_output = completed.stderr.strip() or completed.stdout.strip() or "Unknown LaTeX error"
            raise RuntimeError(f"PDF generation failed: {error_output}")

        pdf_path = temp_path / "solution.pdf"
        if not pdf_path.exists():
            raise FileNotFoundError("The PDF file was not created in the temporary output directory.")

        # Read the generated PDF back as bytes for the HTTP response.
        return pdf_path.read_bytes()


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
            "content": f"Solve step by step, with full justification, in LaTeX:\n\n{problem_text}\n\nHere are the instructions I want you to follow:\n\n{read_prompt()}",
        }],
    )
    return resp["message"]["content"]


async def render_pdf(solution_latex: str, out_name: str) -> str:
    """Generate a PDF file at the requested output path and return that path."""
    pdf_bytes = await asyncio.to_thread(generate_pdf_bytes, solution_latex)
    target_path = Path(out_name).with_suffix(".pdf")
    target_path.write_bytes(pdf_bytes)
    return str(target_path)


async def solve_problem_image(image_path: str, out_name: str):
    """Convenience wrapper to process an image and return the rendered PDF bytes."""
    problem = await asyncio.to_thread(process_image, image_path)
    solution = await asyncio.to_thread(solve, problem)
    pdf_bytes = await asyncio.to_thread(generate_pdf_bytes, solution)
    return pdf_bytes
