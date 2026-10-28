from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from . import auth_views
from . import views
from .health import healthz

urlpatterns = [
    path('healthz', healthz),
    path('', views.index),

    # auth
    path('api/auth/login/', views.LoginView.as_view()),
    path('api/auth/refresh/', TokenRefreshView.as_view()),
    path('api/auth/me/', views.me),

    # data
    path('api/add-candidate/', views.add_candidate),
    path('api/add-job/', views.add_job),
    path('api/run-pipeline/', views.run_pipeline),
    path('api/candidates/', views.get_candidates),
    path('api/jobs/', views.get_jobs),
    path('api/logs/', views.get_logs),
    path('api/parse-resume/', views.parse_resume),
    path('api/search/', views.search),
    path('api/auth/demo/', auth_views.demo_login),
    path('api/auth/signup/', auth_views.signup),
]