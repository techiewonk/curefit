import json
from functools import wraps

from django.contrib import admin
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from scraper import files, jobs
from scraper.models import Session

from . import services
from .services import ConsoleError


def page(template, title):
    """A console page rendered inside the admin layout (sidebar, theme, dark mode)."""
    @admin.site.admin_view
    @ensure_csrf_cookie
    def view(request):
        ctx = {**admin.site.each_context(request), "title": title, "subtitle": None}
        return render(request, template, ctx)
    return view


def api(method="GET"):
    """JSON endpoint: turns our errors into {"error": ...} with the right status code."""
    def deco(fn):
        @admin.site.admin_view
        @(require_POST if method == "POST" else require_GET)
        @wraps(fn)
        def wrapper(request, *a, **kw):
            try:
                body = json.loads(request.body or b"{}") if method == "POST" else {}
                return JsonResponse(fn(request, body, *a, **kw), safe=False)
            except (ConsoleError, jobs.JobError) as e:
                return JsonResponse({"error": str(e)}, status=e.code)
            except (KeyError, ValueError, TypeError) as e:
                return JsonResponse({"error": f"Bad request: {e}"}, status=400)
        return wrapper
    return deco


# ------------------------------------------------------------------ pages
run_page = page("console/run.html", "Run")
monitor_page = page("console/monitor.html", "Live monitor")
analytics_page = page("console/analytics.html", "Analytics")
compare_page = page("console/compare.html", "Compare sessions")
centers_page = page("console/centers.html", "Update centers")


# ------------------------------------------------------------------ api
@api()
def status(request, body):
    return services.status()


@api()
def config(request, body):
    return services.config()


@api()
def estimate(request, body):
    g = request.GET
    return services.estimate(g.get("list", "actual"), [x for x in g.get("only", "").split(",") if x] or None,
                             g.get("workers", 1))


@api()
def unfinished(request, body):
    return services.unfinished(request.GET["start"], request.GET["end"])


@api()
def monitor(request, body):
    return services.monitor(request.GET["session"])


@api()
def sessions(request, body):
    return services.session_choices()


@api()
def analytics(request, body):
    return services.analytics(request.GET["session"])


@api()
def sales_detail(request, body):
    return services.sales_detail(request.GET["session"], request.GET["center_id"])


@api()
def compare(request, body):
    return services.compare(request.GET["a"], request.GET["b"])


@api()
def centers(request, body):
    return services.centers_payload()


@api("POST")
def login_send(request, body):
    return services.send_otp()


@api("POST")
def login_verify(request, body):
    return services.verify_otp(body.get("otp"))


@api("POST")
def run(request, body):
    return services.start_run(body)


@api("POST")
def stop(request, body):
    return services.stop_run()


@api("POST")
def pause(request, body):
    return services.pause_run(bool(body.get("on", True)))


@api("POST")
def centers_sync(request, body):
    return services.sync_centers()


@api("POST")
def centers_list(request, body):
    return services.save_list(body.get("list"), body.get("ids") or [])


@api("POST")
def merge(request, body):
    session = Session.objects.get(pk=body["session"])
    return {"ok": True, "files": [p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in files.merge_session(session)]}


@admin.site.admin_view
@require_GET
def download(request):
    """A file inside a session folder (never outside it)."""
    import os
    session = Session.objects.filter(pk=request.GET.get("session")).first()
    if not session or not session.folder:
        raise Http404("Unknown session")
    root = os.path.realpath(session.folder)
    full = os.path.realpath(os.path.join(root, request.GET.get("path", "")))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        raise Http404("File not found")
    return FileResponse(open(full, "rb"), as_attachment=True, filename=os.path.basename(full))


@api()
def session_files(request, body):
    import os
    session = Session.objects.get(pk=request.GET["session"])
    out = []
    for sub in ("", "merged"):
        d = os.path.join(session.folder, sub)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                p = os.path.join(d, f)
                if os.path.isfile(p):
                    out.append({"path": f"{sub}/{f}" if sub else f, "size": os.path.getsize(p)})
    return {"files": out}
