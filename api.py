
import csv
import io
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import db

app = FastAPI(title="Insolvency Radar")

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

REFRESH_HOURS = float(os.environ.get("RADAR_REFRESH_HOURS", "24"))
AUTO_REFRESH = os.environ.get("RADAR_AUTO_REFRESH", "0") == "1"

_last = {"at": None, "count": 0, "error": None}


def refresh_data():
    from .pipeline import run
    _last["count"] = run()
    _last["error"] = None
    _last["at"] = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime()
    )
    return _last["count"]


def _refresh_loop():
    while True:
        try:
            refresh_data()
        except Exception as e:
            _last["error"] = str(e)
        time.sleep(REFRESH_HOURS * 3600)


@app.on_event("startup")
def start_background_refresh():
    if AUTO_REFRESH:
        threading.Thread(
            target=_refresh_loop, daemon=True
        ).start()


@app.get("/api/health")
def health():
    return {"status": "ok", "application": "Insolvency Radar"}


@app.get("/api/leads")
def leads(
    kind: str | None = None,
    state: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(200, le=1000)
):
    with db.connect() as con:
        return db.search(con, kind, state, status, q, limit)


@app.get("/api/status")
def status():
    return _last


@app.get("/api/export.csv")
def export(kind: str | None = None, state: str | None = None):
    with db.connect() as con:
        rows = db.search(con, kind, state, limit=5000)

    buf = io.StringIO()
    cols = [c for c in db.COLUMNS if c != "raw_text"]

    writer = csv.DictWriter(
        buf, fieldnames=cols, extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(rows)

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="insolvency-radar.csv"'
        }
    )


@app.post("/api/refresh")
def refresh():
    return {"ingested": refresh_data()}


@app.get("/manifest.json")
def manifest():
    path = STATIC / "manifest.json"
    if path.exists():
        return FileResponse(
            path, media_type="application/manifest+json"
        )
    return {"name": "Insolvency Radar"}


@app.get("/sw.js")
def service_worker():
    path = STATIC / "sw.js"
    if path.exists():
        return FileResponse(
            path, media_type="application/javascript"
        )
    return Response(status_code=404)


@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "dashboard.html").read_text(encoding="utf-8")
