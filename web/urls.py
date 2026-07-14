# Web layer removed entirely: no dashboard, REST API, or admin. Django is used only
# for its ORM, migrations and management commands; no HTTP surface is served.
from django.urls import URLPattern, URLResolver

urlpatterns: list[URLPattern | URLResolver] = []
