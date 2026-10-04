import asyncio
import logging

from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import render

from utility_scripts.solver import generate_pdf_bytes, process_image, solve

logger = logging.getLogger(__name__)


def homepage(request):
    return render(request, 'mainapp/homepage.html')


def about(request):
    return render(request, 'mainapp/about.html')


def llms(request):
    return render(request, 'mainapp/llms.html')


def generator(request):
    return render(request, 'mainapp/generator.html')


async def solver(request):
    if request.method != "POST":
        if request.method == "GET":
            return render(request, 'mainapp/solver.html')
        return HttpResponseNotAllowed(["GET", "POST"])

    try:
        action = request.POST.get("action")

        if action == "read":
            uploaded_file = request.FILES.get("fileInput")
            if uploaded_file is None:
                return JsonResponse(
                    {"error": "Please upload an image before submitting."},
                    status=400,
                )

            problem = await asyncio.to_thread(process_image, uploaded_file)
            return JsonResponse({"problem": problem})

        if action == "solve":
            problem = request.POST.get("problem", "").strip()
            if not problem:
                return JsonResponse(
                    {"error": "No transcribed problem was provided."},
                    status=400,
                )

            solution = await asyncio.to_thread(solve, problem)
            return JsonResponse({"solution": solution})

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
        logger.exception("Failed to process solver request")
        return JsonResponse(
            {"error": "The solver could not process your request. Please try again."},
            status=500,
        )