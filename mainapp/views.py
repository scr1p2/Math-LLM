"""HTTP views, including the asynchronous request dispatcher for solver actions."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from queue import Empty, Queue
from threading import Thread

from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse, StreamingHttpResponse
from django.shortcuts import render

from utility_scripts.solver import (
    TEXT_MODEL,
    generate_pdf_bytes,
    process_image_events,
    solve_events,
)

logger = logging.getLogger(__name__)
STREAM_PROGRESS_INTERVAL_SECONDS = 5


async def _stream_solver_events(
    events: Iterator[tuple[str, str]],
    progress_message: str,
) -> AsyncIterator[str]:
    """Encode events asynchronously for ASGI without buffering the stream."""
    iterator = iter(events)
    next_event = asyncio.create_task(asyncio.to_thread(next, iterator, None))
    try:
        while True:
            completed, _ = await asyncio.wait(
                {next_event},
                timeout=STREAM_PROGRESS_INTERVAL_SECONDS,
            )
            if not completed:
                yield f"data: {json.dumps({'type': 'progress', 'text': progress_message})}\n\n"
                continue

            event = next_event.result()
            if event is None:
                break
            event_type, text = event
            yield f"data: {json.dumps({'type': event_type, 'text': text})}\n\n"
            next_event = asyncio.create_task(asyncio.to_thread(next, iterator, None))
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    except Exception as exc:  # pragma: no cover - surfaced to the browser
        logger.exception("Solver event stream failed")
        yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"


def _stream_solver_events_sync(
    events: Iterator[tuple[str, str]],
    progress_message: str,
) -> Iterator[str]:
    """Bridge blocking model calls to a synchronous WSGI stream with heartbeats."""
    event_queue: Queue[tuple[str, tuple[str, str] | Exception | None]] = Queue()

    def produce_events() -> None:
        try:
            for event in events:
                event_queue.put(("event", event))
        except Exception as exc:  # pragma: no cover - surfaced to the browser
            event_queue.put(("error", exc))
        finally:
            event_queue.put(("done", None))

    producer = Thread(target=produce_events, daemon=True)
    producer.start()

    while True:
        try:
            event_type, value = event_queue.get(timeout=STREAM_PROGRESS_INTERVAL_SECONDS)
        except Empty:
            yield f"data: {json.dumps({'type': 'progress', 'text': progress_message})}\n\n"
            continue

        if event_type == "done":
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        if event_type == "error":
            if isinstance(value, Exception):
                logger.error(
                    "Solver event stream failed: %s",
                    value,
                    exc_info=(type(value), value, value.__traceback__),
                )
            yield f"data: {json.dumps({'type': 'error', 'error': str(value)})}\n\n"
            return

        if not isinstance(value, tuple):
            raise TypeError("Solver event stream returned an invalid event.")
        event_name, text = value
        yield f"data: {json.dumps({'type': event_name, 'text': text})}\n\n"


def _stream_response(
    request,
    events: Iterator[tuple[str, str]],
    progress_message: str,
) -> StreamingHttpResponse:
    streaming_content = (
        _stream_solver_events(events, progress_message)
        if hasattr(request, "scope")
        else _stream_solver_events_sync(events, progress_message)
    )
    response = StreamingHttpResponse(
        streaming_content,
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def homepage(request):
    """Render the application's landing page."""
    return render(request, 'mainapp/homepage.html')


def about(request):
    """Render the page describing the application."""
    return render(request, 'mainapp/about.html')


def llms(request):
    """Render the page listing the language models used by the application."""
    return render(request, 'mainapp/llms.html')


def generator(request):
    """Render the separate text-generation page."""
    return render(request, 'mainapp/generator.html')


async def solver(request):
    """Serve the solver page and dispatch its read, solve, and PDF requests.

    GET renders the interface; POST selects an operation using the submitted
    ``action`` field. Model output is delivered as server-sent events, while
    synchronous PDF compilation runs in a worker thread.

    Streaming responses let the UI show image transcription and the generated
    step-by-step solution without blocking the rest of the application.
    """
    if request.method != "POST":
        if request.method == "GET":
            return render(request, 'mainapp/solver.html')
        return HttpResponseNotAllowed(["GET", "POST"])

    try:
        action = request.POST.get("action")

        # Stream the transcription once so the live log and solve request share it.
        if action == "read":
            uploaded_file = request.FILES.get("fileInput")
            if uploaded_file is None:
                return JsonResponse(
                    {"error": "Please upload an image before submitting."},
                    status=400,
                )

            return _stream_response(
                request,
                process_image_events(uploaded_file),
                "Image model is still processing the image...",
            )

        # Stream the solution once so the live log and final result share output.
        if action == "solve":
            problem = request.POST.get("problem", "").strip()
            if not problem:
                return JsonResponse(
                    {"error": "No transcribed problem was provided."},
                    status=400,
                )

            return _stream_response(
                request,
                solve_events(problem),
                f"{TEXT_MODEL} is reasoning; waiting for its next answer chunk...",
            )

        # Render the solution as a PDF response for inline viewing.
        if action == "pdf":
            solution = request.POST.get("solution", "").strip()
            if not solution:
                return JsonResponse(
                    {"error": "No solution was provided to generate a PDF."},
                    status=400,
                )

            try:
                pdf_bytes = await asyncio.to_thread(generate_pdf_bytes, solution)
            except RuntimeError as exc:
                logger.warning("Solver PDF generation failed: %s", exc)
                return JsonResponse({"error": str(exc)}, status=422)
            response = HttpResponse(pdf_bytes, content_type="application/pdf")
            response["Content-Disposition"] = 'inline; filename="solution.pdf"'
            return response

        return JsonResponse({"error": "Unsupported solver action."}, status=400)
    except Exception:
        # Keep the client response generic while retaining details in server logs.
        logger.exception("Failed to process solver request")
        return JsonResponse(
            {"error": "The solver could not process your request. Please try again."},
            status=500,
        )