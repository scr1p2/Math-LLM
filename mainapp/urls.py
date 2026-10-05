"""Named URL routes for pages and actions owned by the main application."""

from django.urls import path 

from . import views 

urlpatterns = [
    path('', views.homepage, name='homepage'),
    path('about/', views.about, name='about'),
    path('llms/', views.llms, name='llms'),
    path('generator/', views.generator, name='generator'),
    path('solver/', views.solver, name='solver')
]