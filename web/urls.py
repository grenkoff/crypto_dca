from django.contrib import admin
from django.urls import include, path

from web.api.health import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("api/v1/", include("web.api.urls")),
    path("", include("web.dashboard.urls")),
]
