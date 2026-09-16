from django.urls import path 

from . import views 

urlpatterns = [
    path('', views.homepage, name='homepage'),
    path('generator/', views.generator, name='generator'),
    path('solver/', views.solver, name='solver')
]