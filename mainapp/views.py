import asyncio

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

from utility_scripts.solver import generate_pdf_bytes, process_image, solve


def homepage(request):
    return render(request, 'mainapp/homepage.html')


def about(request):
    return render(request, 'mainapp/about.html')


def llms(request):
    return render(request, 'mainapp/llms.html')


def generator(request):
    return render(request, 'mainapp/generator.html')


async def solver(request):
    if request.method == "POST":
        try:
            action = request.POST.get("action")

            if action == "solve":
                problem = request.POST.get("problem", "").strip()
                if not problem:
                    return JsonResponse({"error": "No transcribed problem was provided."}, status=400)
            else:
                uploaded_file = request.FILES.get('fileInput')
                if uploaded_file is None:
                    return JsonResponse({"error": "Please upload an image before submitting."}, status=400)

                problem = await asyncio.to_thread(process_image, uploaded_file)
                if action == "read":
                    return JsonResponse({"problem": problem})

            solution = await asyncio.to_thread(solve, problem)
            pdf_bytes = generate_pdf_bytes(solution)

            response = HttpResponse(pdf_bytes, content_type="application/pdf")
            response["Content-Disposition"] = 'inline; filename="solution.pdf"'
            return response

        except Exception as exc:
            return JsonResponse({"error": str(exc)}, status=500)

    return render(request, 'mainapp/solver.html')