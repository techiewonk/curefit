"""
Runs one scrape job in its own process:  python worker.py <job-id>

The web admin writes runs/.jobs/<id>.spec.json and starts this. Progress goes to
runs/.jobs/<id>.json (rewritten about once a second, with a heartbeat), and the admin asks it
to stop by creating runs/.jobs/<id>.stop. Because this is a separate process, restarting the
web server never interrupts the scrape.
"""
import os
import sys
import threading
import time

import admin_core as A
import scrape_sales as S


def run(jid):
    jobs = A.jobs_dir()
    spec = A.read_json(os.path.join(jobs, f"{jid}.spec.json"))
    if spec is None:
        raise SystemExit(f"No job spec for {jid}")
    status_path = os.path.join(jobs, f"{jid}.json")
    stop_path = os.path.join(jobs, f"{jid}.stop")
    status = {"id": jid, "state": "running", "pid": os.getpid(), "session": None, "folder": None,
              "done": 0, "total": 0, "desc": "starting…", "log": [], "stop": False, "error": None}
    finished = threading.Event()

    def publish():
        snap = dict(status)
        snap["log"] = list(status["log"])[-200:]
        snap["updated_at"] = time.time()
        A.write_json_atomic(status_path, snap)

    def heartbeat():
        while not finished.wait(1.0):
            if os.path.exists(stop_path):
                status["stop"] = True
            try:
                publish()
            except OSError:
                pass

    publish()
    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        session = A.session_info(spec["session"]) if spec.get("session") else None
        S.run_scrape(spec["start"], spec["end"], centers_csv=spec["centers_csv"], db=spec["db"],
                     only=spec.get("only"), session=session, status=status,
                     workers=spec.get("workers", 1))
    except Exception as e:  # report it to the UI instead of dying silently
        status.update(state="error", error=str(e))
    finally:
        finished.set()
        if status["state"] == "running":  # run_scrape normally sets "done" itself
            status["state"] = "done"
        publish()


if __name__ == "__main__":
    run(sys.argv[1])
