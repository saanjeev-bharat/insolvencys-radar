
import csv
import io
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field
import jwt
import auth
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

import db

app = FastAPI(title="Insolvency Radar")

# Customer authentication configuration

security = HTTPBearer(auto_error=False)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
class WatchlistRequest(BaseModel):
    acn: str = Field(pattern=r"^\d{9}$")
    company_name: str | None = Field(default=None, max_length=250)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
):
    """Verify the login token and retrieve the active customer."""

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required."
        )

    try:
        payload = auth.decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])

    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired login token."
        )

    with db.connect() as con:
        user = con.execute(
            """
            SELECT id, email, plan, is_active
            FROM users
            WHERE id=?
            """,
            (user_id,)
        ).fetchone()

    if user is None or not user["is_active"]:
        raise HTTPException(
            status_code=401,
            detail="Customer account is unavailable."
        )

    return dict(user)

@app.post("/api/auth/register")
def register_customer(data: RegisterRequest):
    """Register a new customer account."""

    email = str(data.email).strip().lower()

    try:
        password_hash = auth.hash_password(data.password)

        with db.connect() as con:
            inserted = con.execute(
                """
                INSERT INTO users (email, password_hash, plan)
                VALUES (?, ?, 'free')
                RETURNING id
                """,
                (email, password_hash)
            ).fetchone()

            user_id = inserted[0]

    except db.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists."
        )

    token = auth.create_access_token(user_id, email)

    return {
        "token": token,
        "user": {
            "id": user_id,
            "email": email,
            "plan": "free"
        }
    }


@app.post("/api/auth/login")
def login_customer(data: LoginRequest):
    """Authenticate an existing customer."""

    email = str(data.email).strip().lower()

    with db.connect() as con:



       user = con.execute(
            """
            SELECT id, email, password_hash, plan, is_active
            FROM users
            WHERE LOWER(email)=LOWER(?)
            """,
            (email,)
        ).fetchone()

    if (
        user is None
        or not user["is_active"]
        or not auth.verify_password(data.password, user["password_hash"])
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password."
        )

    token = auth.create_access_token(user["id"], user["email"])

    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "plan": user["plan"]
        }
    }


@app.get("/api/auth/me")
def current_customer(user: dict = Depends(get_current_user)):
    """Return the currently authenticated customer."""

    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "plan": user["plan"]
        }
    }
# Customer company watchlist

PLAN_LIMITS = {
    "free": 5,
    "starter": 50,
    "professional": 250,
    "business": 1000,
}


@app.get("/api/watchlist")
def get_watchlist(user: dict = Depends(get_current_user)):

    limit = PLAN_LIMITS.get(user["plan"], 5)

    with db.connect() as con:
        rows = con.execute(
            """
            SELECT acn, company_name
            FROM watchlists
            WHERE user_id=?
            ORDER BY created_at DESC
            """,
            (user["id"],)
        ).fetchall()

    items = [
        {
            "acn": row["acn"],
            "company_name": row["company_name"] or "Unknown company"
        }
        for row in rows
    ]

    return {
        "items": items,
        "limit": limit
    }


@app.post("/api/watchlist")
def add_watchlist(
    data: WatchlistRequest,
    user: dict = Depends(get_current_user)
):

    limit = PLAN_LIMITS.get(user["plan"], 5)
    acn = data.acn.strip()
    company_name = (
        data.company_name.strip()
        if data.company_name else None
    )

    with db.connect() as con:
        db.begin_write(con, user["id"])
        existing = con.execute(
            """
            SELECT id FROM watchlists
            WHERE user_id=? AND acn=?
            """,
            (user["id"], acn)
        ).fetchone()

        if existing:
            return {"message": "Company already saved."}

        count = con.execute(
            "SELECT COUNT(*) FROM watchlists WHERE user_id=?",
            (user["id"],)
        ).fetchone()[0]

        if count >= limit:
            raise HTTPException(
                status_code=403,
                detail=f"Your {user['plan']} plan allows {limit} monitored companies."
            )

        con.execute(
            """
            INSERT INTO watchlists (user_id, acn, company_name)
            VALUES (?, ?, ?)
            """,
            (user["id"], acn, company_name)
        )

    return {"message": "Company saved successfully."}


@app.delete("/api/watchlist/{acn}")
def remove_watchlist(
    acn: str,
    user: dict = Depends(get_current_user)
):

    if not acn.isdigit() or len(acn) != 9:
        raise HTTPException(
            status_code=400,
            detail="Invalid Australian ACN."
        )

    with db.connect() as con:
        cursor = con.execute(
            """
            DELETE FROM watchlists
            WHERE user_id=? AND acn=?
            """,
            (user["id"], acn)
        )

        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail="Company not found in your watchlist."
            )

    return {"message": "Company removed successfully."}
HERE = Path(__file__).resolve().parent

REFRESH_HOURS = float(os.environ.get("RADAR_REFRESH_HOURS", "24"))
AUTO_REFRESH = os.environ.get("RADAR_AUTO_REFRESH", "0") == "1"

_last = {"at": None, "count": 0, "error": None}

# Frontend access configuration
origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def refresh_data():
    from pipeline import run

    count = run()

    _last["count"] = count
    _last["error"] = None
    _last["at"] = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC",
        time.gmtime()
    )

    return count


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
            target=_refresh_loop,
            daemon=True
        ).start()


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "application": "Insolvency Radar"
    }


@app.get("/api/leads")
def leads(
    kind: str | None = None,
    state: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=20000)
):
    with db.connect() as con:
        return db.search(
            con,
            kind=kind,
            state=state,
            status=status,
            q=q,
            limit=limit
        )


@app.get("/api/status")
def status():
    return _last


@app.get("/api/export.csv")
def export(
    kind: str | None = None,
    state: str | None = None
):
    with db.connect() as con:
        rows = db.search(
            con,
            kind=kind,
            state=state,
            limit=5000
        )

    buf = io.StringIO()
    cols = [c for c in db.COLUMNS if c != "raw_text"]

    writer = csv.DictWriter(
        buf,
        fieldnames=cols,
        extrasaction="ignore"
    )

    writer.writeheader()
    writer.writerows(rows)

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition":
            'attachment; filename="insolvency-radar.csv"'
        }
    )


@app.post("/api/refresh")
def refresh():
    return {"ingested": refresh_data()}


@app.get("/manifest.json")
def manifest():
    path = HERE / "manifest.json"

    if path.exists():
        return FileResponse(
            path,
            media_type="application/manifest+json"
        )

    return {"name": "Insolvency Radar"}


@app.get("/sw.js")
def service_worker():
    path = HERE / "sw.js"

    if path.exists():
        return FileResponse(
            path,
            media_type="application/javascript"
        )

    return Response(status_code=404)


@app.get("/", response_class=HTMLResponse)
def index():
    path = HERE / "dashboard.html"

    return path.read_text(encoding="utf-8")
