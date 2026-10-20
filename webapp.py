"""
Curefit admin as a FastAPI web app — this PC only.

    python webapp.py                  run at http://localhost:8000
    python webapp.py --reload         development: restarts on code changes (scrapes keep
                                      running, they live in their own worker process)
    python webapp.py --port 9000 --db curefit.db --no-open

API docs for the endpoints: http://localhost:8000/docs

It listens on 127.0.0.1 only. Because it can start scrapes, every change (POST) needs a
secret token (kept in runs/.admin_secret, injected into the page) and requests from other
sites are refused.
"""

import argparse
import os
import secrets
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

import admin_core as A
import curefit_login as C
import scrape_sales as S

C.QUIET = True  # login helpers must never print tokens or cookies
INDEX = os.path.join(A.HERE, "web", "index.html")
STARTED = time.time()
LOCAL_HOSTS = ("localhost", "127.0.0.1")


@asynccontextmanager
async def lifespan(_app):
    A.init_db()  # databases from before sessions get their session columns
    yield


app = FastAPI(title="Curefit Admin", lifespan=lifespan)


# ------------------------------ security + errors ------------------------------
@app.middleware("http")
async def guard(request: Request, call_next):
    """Only this machine, and for changes only this page (DNS-rebinding / CSRF defence)."""
    host = request.headers.get("host", "")
    hostname, _, port = host.partition(":")
    if hostname not in LOCAL_HOSTS:
        return JSONResponse({"error": "Forbidden host"}, 403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin not in {
            f"http://{h}:{port}" if port else f"http://{h}" for h in LOCAL_HOSTS
        }:
            return JSONResponse({"error": "Forbidden origin"}, 403)
        if not secrets.compare_digest(
            request.headers.get("x-admin-token", ""), A.admin_token()
        ):
            return JSONResponse(
                {"error": "Missing or wrong admin token (reload the page)"}, 403
            )
    return await call_next(request)


@app.exception_handler(A.ApiError)
async def api_error(_request, exc: A.ApiError):
    return JSONResponse({"error": str(exc)}, exc.code)


@app.exception_handler(RequestValidationError)
async def bad_request(_request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    return JSONResponse(
        {"error": f"Bad request: {where} {first.get('msg', '')}".strip()}, 400
    )


@app.exception_handler(Exception)
async def server_error(_request, exc: Exception):
    return JSONResponse({"error": str(exc)}, 500)


# ------------------------------ request bodies ------------------------------
class OtpBody(BaseModel):
    otp: str = ""


class RunBody(BaseModel):
    start: str = ""
    end: str = ""
    session: Optional[str] = None
    list_name: str = Field("actual", alias="list")
    only: Optional[List[str]] = None
    workers: int = 1
    retry_empty: bool = False


class PauseBody(BaseModel):
    on: bool = True


class SessionBody(BaseModel):
    session: str


class ListBody(BaseModel):
    list_name: str = Field(alias="list")
    ids: List[str]


# ------------------------------ pages + reads ------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    with open(
        INDEX, encoding="utf-8"
    ) as f:  # read per request: HTML edits show on refresh
        html = f.read().replace("__TOKEN__", A.admin_token())
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health():
    return {"ok": True, "pid": os.getpid(), "started": STARTED}


@app.get("/api/home")
def home():
    return A.home_payload()


@app.get("/api/estimate")
def estimate(list: str = "actual", only: str = "", workers: int = 1):
    return A.estimate_payload(list, [x for x in only.split(",") if x] or None, workers)


@app.get("/api/monitor")
def monitor(session: str):
    return A.monitor_payload(session)


@app.get("/api/config")
def config():
    return A.config_payload()


@app.get("/api/status")
def status(fresh: int = 0):
    return {
        "logged_in": A.logged_in(bool(fresh)),
        "email": C.EMAIL,
        "job": A.job_payload(),
    }


@app.get("/api/job")
def job():
    return A.job_payload()


@app.get("/api/sessions")
def sessions():
    return A.sessions_payload()


@app.get("/api/session_files")
def session_files(session: str):
    return A.session_files(session)


@app.get("/api/unfinished")
def unfinished(start: str, end: str):
    u = S.find_unfinished(start, end)
    return {
        "session": u["session_id"] if u else None,
        "created_at": u["created_at"] if u else None,
    }


@app.get("/api/periods")
def periods():
    return A.list_periods()


@app.get("/api/analysis")
def analysis(session: str, start: str, end: str):
    return A.analysis_payload(session, start, end)


@app.get("/api/sales")
def sales(session: str, start: str, end: str, center_id: str):
    return A.sales_detail(session, start, end, center_id)


@app.get("/api/centers")
def centers():
    return A.centers_payload()


@app.get("/download")
def download(session: str, path: str):
    full = A.download_path(session, path)
    return FileResponse(full, filename=os.path.basename(full))


# ------------------------------ actions ------------------------------
@app.post("/api/login/send")
def login_send():
    return A.send_otp()


@app.post("/api/login/verify")
def login_verify(body: OtpBody):
    return A.verify_otp(body.otp)


@app.post("/api/run")
def run(body: RunBody):
    return A.start_job(body.model_dump(by_alias=True))


@app.post("/api/pause")
def pause(body: PauseBody):
    return A.pause_job(body.on)


@app.post("/api/stop")
def stop():
    return A.stop_job()


@app.post("/api/merge")
def merge(body: SessionBody):
    return {"ok": True, "files": A.merge_session(body.session)}


@app.post("/api/centers/sync")
def centers_sync():
    return A.sync_centers_api()


@app.post("/api/centers/list")
def centers_list(body: ListBody):
    return A.save_list_api(body.list_name, body.ids)


# ------------------------------ launcher ------------------------------
def main():
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument(
        "--db", help="database file (default: curefit.db next to this script)"
    )
    ap.add_argument(
        "--reload",
        action="store_true",
        help="development: restart the server when a .py file changes",
    )
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    if args.db:
        os.environ["CUREFIT_DB"] = (
            args.db if os.path.isabs(args.db) else os.path.join(A.HERE, args.db)
        )

    url = f"http://localhost:{args.port}/"
    print(
        f"Curefit admin: {url}   docs: {url}docs"
        + ("   (auto-reload on)" if args.reload else "")
    )
    print("Press Ctrl+C to stop. A running scrape keeps going if the server restarts.")
    if not args.no_open:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        "webapp:app",
        host="127.0.0.1",
        port=args.port,
        reload=args.reload,
        reload_dirs=[A.HERE] if args.reload else None,
        reload_includes=["*.py"] if args.reload else None,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
