from django.urls import path

from web.api import views

app_name = "api"

urlpatterns = [
    path("status/", views.status, name="status"),
    path("positions/", views.positions, name="positions"),
    path("pnl/", views.pnl, name="pnl"),
    path("config/", views.config, name="config"),
    path("bot/pause/", views.pause, name="pause"),
    path("bot/resume/", views.resume, name="resume"),
]
