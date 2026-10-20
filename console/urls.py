from django.urls import path

from . import views

app_name = "console"

urlpatterns = [
    path("run/", views.run_page, name="run"),
    path("monitor/", views.monitor_page, name="monitor"),
    path("analytics/", views.analytics_page, name="analytics"),
    path("compare/", views.compare_page, name="compare"),
    path("centers/", views.centers_page, name="centers"),
    path("download/", views.download, name="download"),
    # json api used by the pages
    path("api/status/", views.status),
    path("api/config/", views.config),
    path("api/estimate/", views.estimate),
    path("api/unfinished/", views.unfinished),
    path("api/monitor/", views.monitor),
    path("api/sessions/", views.sessions),
    path("api/session-files/", views.session_files),
    path("api/analytics/", views.analytics),
    path("api/sales/", views.sales_detail),
    path("api/compare/", views.compare),
    path("api/centers/", views.centers),
    path("api/login/send/", views.login_send),
    path("api/login/verify/", views.login_verify),
    path("api/run/", views.run),
    path("api/stop/", views.stop),
    path("api/pause/", views.pause),
    path("api/merge/", views.merge),
    path("api/centers/sync/", views.centers_sync),
    path("api/centers/list/", views.centers_list),
]
