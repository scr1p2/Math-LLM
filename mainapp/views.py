from django.shortcuts import render

# Create your views here.
def homepage(request):
    return render(request, 'mainapp/homepage.html')

def generator(request):
    return render(request, 'mainapp/generator.html')

def solver(request):
    return render(request, 'mainapp/solver.html')