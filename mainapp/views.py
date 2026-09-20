from django.http import JsonResponse
from django.shortcuts import render

from utility_scripts.image_partitioning import process_single_image_with_ollama


# Create your views here.
def homepage(request):
    return render(request, 'mainapp/homepage.html')


def generator(request):
    return render(request, 'mainapp/generator.html')


def solver(request):
    if request.method == "POST":
        uploaded_file = request.FILES.get('fileInput')

        if uploaded_file is None:
            return JsonResponse({"error": "No file uploaded."}, status=400)

        try:
            extracted_text = process_single_image_with_ollama(uploaded_file)

            if not extracted_text:
                return JsonResponse(
                    {"text": "", "message": "No readable text detected in the image."},
                    status=200,
                )

            print(extracted_text)

            return JsonResponse({"text": extracted_text}, status=200)
        except Exception as exc:
            return JsonResponse({"error": str(exc)}, status=500)

    return render(request, 'mainapp/solver.html')