import csv
import io
import os
import threading
import time

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import db

app = FastAPI(title="Insolvency Radar")
HERE = os.path.dirname(__file__)
STATIC = os.path.join(HERE, "static")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

REFRESH_HOURS = float(os.environ.get("RADAR_REFRESH_HOURS", "24"))
AUTO_REFRESH = os.environ.get("RADAR_AUTO_REFRESH", "1") == "1"
_last = {"at": None, "count": 0, "error": None}


def _refresh_loop():
    """Collect notices now, then again every REFRESH_HOURS. Runs in the background."""
    from .pipeline import run
    while True:
        try:
            _last["count"] = run()
            _last["error"] = None
        except Exception as e:  # keep the loop alive
            _last["error"] = str(e)
        _last["at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        time.sleep(REFRESH_HOURS * 3600)


@app.on_event("startup")
def start_background_refresh():
    if AUTO_REFRESH:
        threading.Thread(target=_refresh_loop, daemon=True).start()


@app.get("/api/leads")
def leads(kind: str | None = None, state: str | None = None, status: str | None = None,
          q: str | None = None, limit: int = Query(200, le=1000)):
    with db.connect() as con:
        return db.search(con, kind, state, status, q, limit)


@app.get("/api/status")
def status():
    return _last


@app.get("/api/export.csv", response_class=PlainTextResponse)
def export(kind: str | None = None, state: str | None = None):
    with db.connect() as con:
        rows = db.search(con, kind, state, limit=5000)
    buf = io.StringIO()
    cols = [c for c in db.COLUMNS if c != "raw_text"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader(); w.writerows(rows)
    return buf.getvalue()


@app.post("/api/refresh")
def refresh():
    from .pipeline import run
    return {"ingested": run()}


@app.get("/manifest.json")
def manifest():
    return FileResponse(os.path.join(STATIC, "manifest.json"), media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="application/javascript")


@app.get("/", response_class=HTMLResponse)
def index():
    return open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read()
