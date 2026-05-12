from django.urls import path

from web.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("config/", views.config, name="config"),
]
