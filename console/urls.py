from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = "console"


def page(view):
    """Pages inside the Unfold layout need to know which admin site they belong to."""
    return view.as_view(admin_site=admin.site)


urlpatterns = [
    path("status/", page(views.StatusView), name="status"),
    path("run/", page(views.RunView), name="run"),
    path("monitor/", page(views.MonitorView), name="monitor"),
    path("analytics/", page(views.AnalyticsView), name="analytics"),
    path("compare/", page(views.CompareView), name="compare"),
    path("coverage/", page(views.CoverageView), name="coverage"),
    path("coverage/run/", views.CoverageRun.as_view(), name="coverage_run"),
    path("perf/", RedirectView.as_view(pattern_name="admin:scraper_centermonth_changelist"), name="perf"),
    path("perf/analysis/", page(views.PerfAnalysisView), name="perf_analysis"),
    path("gym/<int:center_id>/", page(views.GymTrendView), name="gym"),
    path("revenue/", page(views.RevenueView), name="revenue"),
    path("revenue/csv/", views.RevenueCsv.as_view(), name="revenue_csv"),
    path("analytics/csv/", views.AnalyticsCsv.as_view(), name="analytics_csv"),
    path("compare/csv/", views.CompareCsv.as_view(), name="compare_csv"),
    path("centers/", page(views.CentersView), name="centers"),
    # fragments that HTMX swaps into the pages above
    path("run/login/", views.LoginPanel.as_view(), name="login"),
    path("run/job/", views.JobPanel.as_view(), name="job"),
    path("run/job/<str:action>/", views.JobPanel.as_view(), name="job_action"),
    path("run/estimate/", views.EstimatePanel.as_view(), name="estimate"),
    path("run/resume/", views.ResumePanel.as_view(), name="resume"),
    path("monitor/grid/", views.MonitorGrid.as_view(), name="monitor_grid"),
]
