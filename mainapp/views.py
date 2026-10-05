"""HTTP views, including the asynchronous request dispatcher for solver actions."""

import asyncio
import logging

from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import render

from utility_scripts.solver import generate_pdf_bytes, process_image, solve

logger = logging.getLogger(__name__)


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
    ``action`` field. Synchronous model and compiler work runs in worker threads
    to keep the asynchronous Django request handler responsive.
    """
    if request.method != "POST":
        if request.method == "GET":
            return render(request, 'mainapp/solver.html')
        return HttpResponseNotAllowed(["GET", "POST"])

    try:
        action = request.POST.get("action")

        # Convert an uploaded image into editable problem text.
        if action == "read":
            uploaded_file = request.FILES.get("fileInput")
            if uploaded_file is None:
                return JsonResponse(
                    {"error": "Please upload an image before submitting."},
                    status=400,
                )

            problem = await asyncio.to_thread(process_image, uploaded_file)
            return JsonResponse({"problem": problem})

        # Solve the submitted/transcribed problem and return JSON for the UI.
        if action == "solve":
            problem = request.POST.get("problem", "").strip()
            if not problem:
                return JsonResponse(
                    {"error": "No transcribed problem was provided."},
                    status=400,
                )

            solution = await asyncio.to_thread(solve, problem)
            return JsonResponse({"solution": solution})

        # Render the solution as a PDF response for inline viewing.
        if action == "pdf":
            solution = request.POST.get("solution", "").strip()
            if not solution:
                return JsonResponse(
                    {"error": "No solution was provided to generate a PDF."},
                    status=400,
                )

            pdf_bytes = await asyncio.to_thread(generate_pdf_bytes, solution)
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