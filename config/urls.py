from django.contrib import admin
from django.templatetags.static import static
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/console/", include("console.urls")),
    path("admin/", admin.site.urls),
    path("favicon.ico", RedirectView.as_view(url=static("console/favicon.svg"))),
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
]
