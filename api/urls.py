from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from . import auth_views
from . import leaderboard
from . import views
from .health import healthz
from . import cv_views

urlpatterns = [
    path('healthz', healthz),
    path('', views.index),

    # auth
    path('api/auth/login/', views.LoginView.as_view()),
    path('api/auth/refresh/', TokenRefreshView.as_view()),
    path('api/auth/me/', views.me),
    path('api/auth/config/', auth_views.auth_config),

    # data
    path('api/add-candidate/', views.add_candidate),
    path('api/add-job/', views.add_job),
    path('api/run-pipeline/', views.run_pipeline),
    path('api/candidates/', views.get_candidates),
    path('api/candidates/<int:pk>/', views.delete_candidate),
    path('api/jobs/', views.get_jobs),
    path('api/jobs/<int:pk>/', views.delete_job),
    path('api/logs/', views.get_logs),
    path('api/parse-resume/', views.parse_resume),
    path('api/search/', views.search),
    path('api/auth/demo/', auth_views.demo_login),
    path('api/auth/signup/', auth_views.signup),
    path('api/auth/verify-otp/', auth_views.verify_otp),
    path('api/auth/resend-otp/', auth_views.resend_otp),
    path('api/auth/profile/', auth_views.profile),
    path('api/auth/clear-data/', auth_views.clear_data),
    path('api/auth/demo/end/', auth_views.demo_end),
    path('api/jobs/<int:pk>/leaderboard/', leaderboard.job_leaderboard_view),
    path('api/match-cv/', cv_views.match_cv_view),
    path('api/auth/forgot-password/', auth_views.forgot_password),
    path('api/auth/reset-password/', auth_views.reset_password),
]