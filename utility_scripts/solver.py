"""Image transcription, math solving, and LaTeX-to-PDF helpers for the solver."""

import asyncio
import base64
import io
import re
import subprocess
import tempfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from threading import Event
from typing import Any, Iterator, NoReturn

import httpx
from PIL import Image, ImageOps
from ollama import Client

# Models tried in order when an uploaded image must be transcribed.
VISION_MODELS = ["qwen2.5vl"]
# Model used to produce the written, step-by-step math solution.
TEXT_MODEL = "deepseek-r1:latest"
# Bound external LaTeX compilation so a request cannot wait indefinitely.
PDFLATEX_TIMEOUT_SECONDS = 120
MAX_IMAGE_BYTES = 2_000_000
MAX_IMAGE_DIMENSION = 2048
MIN_IMAGE_QUALITY = 40


def _raise_ollama_connection_error(exc: httpx.ConnectError) -> NoReturn:
    raise RuntimeError(
        "Could not connect to Ollama. Make sure the Ollama app is running "
        "(or run `ollama serve`) and that OLLAMA_HOST points to its API "
        "(default: http://localhost:11434)."
    ) from exc


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
        if hasattr(image, "seek"):
            try:
                image.seek(0)
            except (AttributeError, OSError):
                pass
        return bytes(data) if isinstance(data, (bytes, bytearray)) else data

    if isinstance(image, str):
        with open(image, "rb") as image_file:
            return image_file.read()

    raise TypeError(f"Unsupported image type: {type(image)!r}")


def _resize_image_bytes(image_bytes: bytes, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    """Shrink an uploaded image until it fits Ollama's payload limits.

    Large math scans can be several MB in size, which causes some local Ollama
    installations to reject the request with a 400/"entity too large" response.
    The image is converted to JPEG and progressively downscaled until the payload
    is safely under the configured byte limit.
    """
    if len(image_bytes) <= max_bytes:
        return image_bytes

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")

            for quality in range(90, MIN_IMAGE_QUALITY - 1, -10):
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=quality, optimize=True)
                encoded = buffer.getvalue()
                if len(encoded) <= max_bytes:
                    return encoded

                if image.width > MAX_IMAGE_DIMENSION or image.height > MAX_IMAGE_DIMENSION:
                    scale = MAX_IMAGE_DIMENSION / max(image.width, image.height)
                    resized = image.resize(
                        (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                        Image.Resampling.LANCZOS,
                    )
                    image = resized
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=quality, optimize=True)
                    encoded = buffer.getvalue()
                    if len(encoded) <= max_bytes:
                        return encoded

            buffer = io.BytesIO()
            image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS)
            image.save(buffer, format="JPEG", quality=MIN_IMAGE_QUALITY, optimize=True)
            encoded = buffer.getvalue()
            if len(encoded) <= max_bytes:
                return encoded

    except Exception:
        pass

    raise ValueError(
        "The uploaded image is too large to send to the vision model. "
        "Please upload a smaller or lower-resolution image."
    )


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
        diagnostic = next(
            (line.strip() for line in error_output.splitlines() if line.lstrip().startswith("!")),
            "Unknown LaTeX error",
        )
        line_match = re.search(r"(?m)^l\.(\d+)", error_output)
        location = f" at generated LaTeX line {line_match.group(1)}" if line_match else ""
        raise RuntimeError(
            f"PDF generation failed{location}: {diagnostic} "
            "Check math delimiters and special characters in the generated solution."
        )

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


_MATH_ENVIRONMENTS = {
    "align",
    "align*",
    "displaymath",
    "equation",
    "equation*",
    "gather",
    "gather*",
    "math",
    "multline",
    "multline*",
}


def _escape_latex_text_specials(solution_latex: str) -> str:
    """Escape raw TeX-special text characters outside common math regions."""
    escaped = []
    in_math = False
    index = 0

    while index < len(solution_latex):
        character = solution_latex[index]

        if character == "\\":
            environment = re.match(
                r"\\(begin|end)\{([^{}]+)\}",
                solution_latex[index:],
            )
            if environment:
                command, name = environment.groups()
                escaped.append(environment.group(0))
                if name in _MATH_ENVIRONMENTS:
                    in_math = command == "begin"
                index += len(environment.group(0))
                continue

            if solution_latex.startswith(r"\(", index):
                in_math = True
                escaped.append(r"\(")
                index += 2
                continue
            if solution_latex.startswith(r"\)", index):
                in_math = False
                escaped.append(r"\)")
                index += 2
                continue
            if solution_latex.startswith(r"\[", index):
                in_math = True
                escaped.append(r"\[")
                index += 2
                continue
            if solution_latex.startswith(r"\]", index):
                in_math = False
                escaped.append(r"\]")
                index += 2
                continue

            escaped.append(character)
            if index + 1 < len(solution_latex):
                escaped.append(solution_latex[index + 1])
                index += 2
            else:
                index += 1
            continue

        if character == "$":
            delimiter = "$$" if solution_latex.startswith("$$", index) else "$"
            in_math = not in_math
            escaped.append(delimiter)
            index += len(delimiter)
            continue

        if not in_math and character in {"_", "%", "&", "#"}:
            escaped.append("\\" + character)
        else:
            escaped.append(character)
        index += 1

    return "".join(escaped)


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

    A small block is added to the top of the rendered PDF so the user can see
    when the solution started, when it ended, and how long it took to produce.
    """
    if not solution_latex:
        raise ValueError("No solution content was provided to generate a PDF.")

    started_at = datetime.now()
    title, solution_without_title = _extract_latex_title(solution_latex)
    title = title or "Step-by-Step Mathematical Solution"
    solution_body = _solution_document_body(solution_without_title)
    if not solution_body:
        raise ValueError("The solution contains no LaTeX document body to render.")
    solution_body = _escape_latex_text_specials(solution_body)

    finished_at = datetime.now()
    elapsed_seconds = round((finished_at - started_at).total_seconds(), 2)
    timestamp_start = started_at.strftime("%Y-%m-%d %H:%M:%S")
    timestamp_end = finished_at.strftime("%Y-%m-%d %H:%M:%S")

    # Include the timing metadata at the top of the document so users can see the
    # exact start/end times and the total render time in the final PDF.
    metadata_block = "\n".join(
        (
            r"\noindent\textbf{Start time: " + timestamp_start + r"}",
            r"\noindent\textbf{End time: " + timestamp_end + r"}",
            r"\noindent\textbf{Elapsed: " + str(elapsed_seconds) + r" seconds}",
            r"\vspace{0.5em}",
        )
    )

    # Build a controlled preamble so model-generated package/class declarations
    # cannot conflict with the document structure maintained by this module.
    document = "\n".join(
        (
            r"\documentclass{article}",
            r"\pdfcompresslevel=9",
            r"\pdfobjcompresslevel=3",
            r"\usepackage[margin=1in]{geometry}",
            r"\usepackage{amsmath,amssymb}",
            r"\title{" + title + "}",
            r"\author{}",
            r"\begin{document}",
            r"\maketitle",
            metadata_block,
            solution_body,
            r"\end{document}",
        )
    )

    with tempfile.TemporaryDirectory(prefix="mathllm_") as temp_dir:
        tex_path = Path(temp_dir) / "solution.tex"
        tex_path.write_text(document, encoding="utf-8")
        return _compile_pdf(tex_path)


def stream_ollama_text(
    response_iter: Iterator[Any],
    *,
    prefix: str = "",
    cancellation_event: Event | None = None,
) -> Iterator[str]:
    """Normalize Ollama streaming chunks into a plain-text stream for the UI.

    The Ollama Python client exposes each streaming item as a response object, not
    always a plain dict. Some chunks also carry partial content fragments, so this
    function accepts both object-based and dictionary-based payloads before
    emitting any text to the browser.
    """
    iterator = iter(response_iter)
    try:
        while cancellation_event is None or not cancellation_event.is_set():
            try:
                chunk = next(iterator)
            except StopIteration:
                return
            if cancellation_event is not None and cancellation_event.is_set():
                return

            content = ""
            if isinstance(chunk, dict):
                message = chunk.get("message", {}) or {}
                if isinstance(message, dict):
                    content = message.get("content", "") or ""
            else:
                message = getattr(chunk, "message", None)
                if isinstance(message, dict):
                    content = message.get("content", "") or ""
                else:
                    content = getattr(message, "content", "") or ""

            if content:
                yield prefix + content
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


def _image_to_ollama_payload(image: Any) -> str:
    """Prepare the image payload for Ollama while keeping file size in check.

    The backend compresses large scans before sending them to the model because a
    raw image payload can trigger 400 responses from local Ollama instances even
    when the request itself is otherwise valid.
    """
    original_bytes = ensure_image_bytes(image)
    image_bytes = _resize_image_bytes(original_bytes)
    return base64.b64encode(image_bytes).decode("utf-8")


def process_image_events(
    image: Any,
    *,
    cancellation_event: Event | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield status and transcription events for an uploaded math problem."""
    yield ("status", "Preparing the uploaded image for the vision model...")
    if cancellation_event is not None and cancellation_event.is_set():
        return
    img_b64 = _image_to_ollama_payload(image)
    client = Client()
    last_error = None

    for model_index, model_name in enumerate(VISION_MODELS, start=1):
        if cancellation_event is not None and cancellation_event.is_set():
            return
        yield (
            "status",
            f"Transcribing with {model_name} "
            f"({model_index}/{len(VISION_MODELS)})...",
        )
        try:
            response_iter = client.chat(
                model=model_name,
                messages=[{
                    "role": "user",
                    "content": "Transcribe this math problem exactly, using LaTeX for all notation.",
                    "images": [img_b64],
                }],
                options={
                    "num_ctx": 8000,
                    "num_predict": 4000,
                },
                stream=True,
            )
            for text in stream_ollama_text(
                response_iter,
                cancellation_event=cancellation_event,
            ):
                yield ("content", text)
            return
        except Exception as exc:  # pragma: no cover - exercised through integration tests
            last_error = exc
            if isinstance(exc, httpx.ConnectError):
                _raise_ollama_connection_error(exc)
            message = str(exc).lower()
            if "too large" in message or "entity too large" in message or "payload too large" in message:
                raise RuntimeError(
                    "The uploaded image is too large for the Ollama model. "
                    "Please upload a smaller image or a lower-resolution scan."
                ) from exc
            if (
                "image input is not supported" in message
                or "mmproj" in message
                or "not supported" in message
            ):
                yield (
                    "status",
                    f"{model_name} does not support image input; trying the next model.",
                )
                continue
            raise

    raise RuntimeError(
        "No Ollama vision model is available for image input. "
        "Install a multimodal model such as `llava:latest` or `qwen2.5vl:7b`, "
        "then start Ollama again."
    ) from last_error


def process_image(
    image: Any,
    *,
    cancellation_event: Event | None = None,
) -> Iterator[str]:
    """Stream one vision-model transcription of an uploaded math problem."""
    for event_type, text in process_image_events(
        image,
        cancellation_event=cancellation_event,
    ):
        if event_type == "content":
            yield text


def solve(
    problem_text: str,
    *,
    cancellation_event: Event | None = None,
) -> Iterator[str]:
    """Stream one detailed, user-facing LaTeX solution from the text model.

    Each streamed text chunk is both displayed in the live status log and
    accumulated by callers that need the complete solution, such as PDF creation.
    """
    try:
        if cancellation_event is not None and cancellation_event.is_set():
            return
        client = Client()
        response_iter = client.chat(
            model=TEXT_MODEL,
            messages=[{
                "role": "user",
                "content": f"Give a clear, step-by-step mathematical derivation with concise justifications and explain the concepts used. Show the user-facing solution, not private internal thoughts. Write in LaTeX and return only the content that belongs inside a LaTeX document body; do not include a document class, package imports, document environment, or Markdown code fences.\n\n{problem_text}\n\nHere are the instructions I want you to follow:\n\n{read_prompt()}",
            }],
            stream=True,
        )
        yield from stream_ollama_text(
            response_iter,
            cancellation_event=cancellation_event,
        )
    except httpx.ConnectError as exc:
        _raise_ollama_connection_error(exc)


def solve_events(
    problem_text: str,
    *,
    cancellation_event: Event | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield startup status and user-facing solution content as it is generated."""
    yield ("status", f"Starting {TEXT_MODEL}...")
    if cancellation_event is not None and cancellation_event.is_set():
        return
    for text in solve(problem_text, cancellation_event=cancellation_event):
        yield ("content", text)


async def solve_problem_to_pdf(problem_text: str) -> bytes:
    """Run the solve-then-render pipeline without blocking the event loop.

    Both model inference and subprocess-based PDF compilation are synchronous
    operations, so each is moved to a worker thread before awaiting its result.
    """
    solution = await asyncio.to_thread(lambda: "".join(solve(problem_text)))
    return await asyncio.to_thread(generate_pdf_bytes, solution)