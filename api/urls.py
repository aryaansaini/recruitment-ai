from django.urls import path
from . import auth_views
from . import leaderboard
from . import views
from .health import healthz
from . import cv_views
from . import board_views
from . import token_views
from . import analytics
from . import interview_views
from . import pipeline_views

urlpatterns = [
    path('healthz', healthz),
    path('', views.index),

    # auth
    path('api/auth/login/', views.LoginView.as_view()),
    path('api/auth/refresh/', token_views.RevocableTokenRefreshView.as_view()),
    path('api/auth/me/', views.me),
    path('api/auth/config/', auth_views.auth_config),

    # data
    path('api/add-candidate/', views.add_candidate),
    path('api/add-job/', views.add_job),
    path('api/run-pipeline/', views.run_pipeline),
    path('api/candidates/', views.get_candidates),
    path('api/candidates/<int:pk>/', views.delete_candidate),
    path('api/candidates/<int:pk>/detail/', board_views.candidate_detail),
    path('api/applications/', board_views.list_applications),
    path('api/applications/<int:pk>/status/', board_views.set_application_status),
    path('api/applications/<int:pk>/interview/', interview_views.schedule_interview),

    # live agent tracking
    path('api/pipeline-runs/', pipeline_views.list_runs),
    path('api/pipeline-runs/start/', pipeline_views.start_run),
    path('api/pipeline-runs/<int:pk>/', pipeline_views.get_run),

    # interview management
    path('api/interviews/', interview_views.list_interviews),
    path('api/interviews/slots/', interview_views.free_slots),
    path('api/interviews/<int:pk>/', interview_views.reschedule_interview),
    path('api/interviews/<int:pk>/cancel/', interview_views.cancel_interview),
    path('api/interviews/<int:pk>/complete/', interview_views.complete_interview),

    # analytics
    path('api/analytics/', analytics.analytics),
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