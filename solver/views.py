from django.shortcuts import render

# Create your views here.
def solver(request):
	return render(request, 'solver/solver.html')
