"""HTTP views, including the asynchronous request dispatcher for solver actions."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from queue import Empty, Queue
from threading import Event, Semaphore, Thread

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
    cancellation_event: Event,
) -> AsyncIterator[str]:
    """Encode model events for ASGI while allowing disconnected clients to cancel."""
    event_queue: Queue[tuple[str, tuple[str, str] | Exception | None]] = Queue()
    event_demand = Semaphore(0)
    _start_event_producer(events, event_queue, event_demand, cancellation_event)
    try:
        while True:
            event_demand.release()
            while True:
                try:
                    event_type, value = await asyncio.to_thread(
                        event_queue.get,
                        True,
                        STREAM_PROGRESS_INTERVAL_SECONDS,
                    )
                    break
                except Empty:
                    yield f"data: {json.dumps({'type': 'progress', 'text': progress_message})}\n\n"

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
            event_type, text = value
            yield f"data: {json.dumps({'type': event_type, 'text': text})}\n\n"
    finally:
        cancellation_event.set()


def _start_event_producer(
    events: Iterator[tuple[str, str]],
    event_queue: Queue[tuple[str, tuple[str, str] | Exception | None]],
    event_demand: Semaphore,
    cancellation_event: Event,
) -> None:
    """Consume a model iterator off-thread and close it when the client disconnects."""
    def produce_events() -> None:
        iterator = iter(events)
        try:
            while not cancellation_event.is_set():
                if not event_demand.acquire(timeout=0.1):
                    continue
                if cancellation_event.is_set():
                    break
                try:
                    event = next(iterator)
                except StopIteration:
                    break
                if not cancellation_event.is_set():
                    event_queue.put(("event", event))
        except Exception as exc:  # pragma: no cover - surfaced to the browser
            if not cancellation_event.is_set():
                event_queue.put(("error", exc))
        finally:
            try:
                close = getattr(iterator, "close", None)
                if close is not None:
                    close()
            except Exception as exc:  # pragma: no cover - cleanup failure
                if not cancellation_event.is_set():
                    event_queue.put(("error", exc))
                else:
                    logger.exception("Failed to close a cancelled solver event stream")
            finally:
                event_queue.put(("done", None))

    Thread(target=produce_events, daemon=True).start()


def _stream_solver_events_sync(
    events: Iterator[tuple[str, str]],
    progress_message: str,
    cancellation_event: Event,
) -> Iterator[str]:
    """Bridge blocking model calls to a synchronous WSGI stream with heartbeats."""
    event_queue: Queue[tuple[str, tuple[str, str] | Exception | None]] = Queue()
    event_demand = Semaphore(0)
    _start_event_producer(events, event_queue, event_demand, cancellation_event)

    try:
        while True:
            event_demand.release()
            while True:
                try:
                    event_type, value = event_queue.get(timeout=STREAM_PROGRESS_INTERVAL_SECONDS)
                    break
                except Empty:
                    yield f"data: {json.dumps({'type': 'progress', 'text': progress_message})}\n\n"

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
    finally:
        cancellation_event.set()


def _stream_response(
    request,
    events: Iterator[tuple[str, str]],
    progress_message: str,
    cancellation_event: Event,
) -> StreamingHttpResponse:
    streaming_content = (
        _stream_solver_events(events, progress_message, cancellation_event)
        if hasattr(request, "scope")
        else _stream_solver_events_sync(events, progress_message, cancellation_event)
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

            cancellation_event = Event()
            return _stream_response(
                request,
                process_image_events(uploaded_file, cancellation_event=cancellation_event),
                "Image model is still processing the image...",
                cancellation_event,
            )

        # Stream the solution once so the live log and final result share output.
        if action == "solve":
            problem = request.POST.get("problem", "").strip()
            if not problem:
                return JsonResponse(
                    {"error": "No transcribed problem was provided."},
                    status=400,
                )

            cancellation_event = Event()
            return _stream_response(
                request,
                solve_events(problem, cancellation_event=cancellation_event),
                f"{TEXT_MODEL} is reasoning; waiting for its next answer chunk...",
                cancellation_event,
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