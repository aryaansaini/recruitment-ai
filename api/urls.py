from django.urls import path
from . import views

urlpatterns = [
    path('', views.index),
    path('api/add-candidate/', views.add_candidate),
    path('api/add-job/', views.add_job),
    path('api/run-pipeline/', views.run_pipeline),
    path('api/candidates/', views.get_candidates),
    path('api/jobs/', views.get_jobs),
    path('api/logs/', views.get_logs),
    path('api/parse-resume/', views.parse_resume),
]