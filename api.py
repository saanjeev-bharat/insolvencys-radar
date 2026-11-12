
import csv
import io
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field
import jwt
import stripe
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
class CheckoutRequest(BaseModel):
    plan: str = Field(pattern=r"^(starter|professional|business)$")

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

@app.post("/api/billing/checkout")
def create_checkout_session(
    data: CheckoutRequest,
    user: dict = Depends(get_current_user)
):
    """Create a Stripe-hosted subscription checkout session."""

    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
    if not stripe.api_key:
        raise HTTPException(
            status_code=503,
            detail="Billing is not configured."
        )

    frontend_url = os.environ.get("FRONTEND_URL", "").strip().rstrip("/")
    if not frontend_url.startswith("https://"):
        raise HTTPException(
            status_code=503,
            detail="Billing frontend URL is not configured correctly."
        )

    price_variables = {
        "starter": "STRIPE_PRICE_STARTER",
        "professional": "STRIPE_PRICE_PROFESSIONAL",
        "business": "STRIPE_PRICE_BUSINESS",
    }
    price_id = os.environ.get(price_variables[data.plan], "").strip()

    if not price_id:
        raise HTTPException(
            status_code=503,
            detail="The selected subscription plan is not configured."
        )

    try:
        with db.connect() as con:
            existing = con.execute(
                """
                SELECT stripe_customer_id
                FROM subscriptions
                WHERE user_id = ?
                  AND stripe_customer_id IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (user["id"],)
            ).fetchone()

        customer_id = (
            existing["stripe_customer_id"]
            if existing else None
        )

        checkout_customer = (
            {"customer": customer_id}
            if customer_id
            else {"customer_email": user["email"]}
        )

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            **checkout_customer,
            client_reference_id=str(user["id"]),
            metadata={
                "user_id": str(user["id"]),
                "plan": data.plan,
            },
            subscription_data={
                "metadata": {
                    "user_id": str(user["id"]),
                    "plan": data.plan,
                }
            },
            success_url=(
                f"{frontend_url}/?billing=success"
                "&session_id={CHECKOUT_SESSION_ID}"
            ),
            cancel_url=f"{frontend_url}/?billing=cancelled",
        )

        return {"checkout_url": session.url}

    except stripe.error.StripeError:
        raise HTTPException(
            status_code=502,
            detail="Unable to create a secure checkout session. Please try again."
        )

def _stripe_timestamp(value):
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


@app.post("/api/billing/webhook")
async def stripe_webhook(request: Request):
    """Verify Stripe events and synchronise subscription access."""

    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

    if not stripe.api_key or not webhook_secret:
        raise HTTPException(
            status_code=503,
            detail="Stripe webhook is not configured."
        )

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, signature, webhook_secret
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(
            status_code=400,
            detail="Invalid Stripe webhook signature."
        )

    event_data = event.to_dict_recursive()
    event_type = event_data.get("type")
    event_object = event_data.get("data", {}).get("object", {})

    subscription_id = None
    checkout_user_id = None
    checkout_customer_id = None

    if event_type == "checkout.session.completed":
        subscription_id = event_object.get("subscription")
        checkout_customer_id = event_object.get("customer")
        checkout_user_id = (
            event_object.get("client_reference_id")
            or event_object.get("metadata", {}).get("user_id")
        )

    elif event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        subscription_id = event_object.get("id")

    elif event_type == "invoice.payment_failed":
        subscription_id = event_object.get("subscription")

        if not subscription_id:
            parent = event_object.get("parent") or {}
            details = parent.get("subscription_details") or {}
            subscription_id = details.get("subscription")

    else:
        return {"received": True, "ignored": True}

    if isinstance(subscription_id, dict):
        subscription_id = subscription_id.get("id")

    if not subscription_id:
        return {"received": True, "ignored": True}

    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
        sub = subscription.to_dict_recursive()

    except stripe.error.StripeError:
        raise HTTPException(
            status_code=502,
            detail="Unable to retrieve Stripe subscription."
        )

    metadata = sub.get("metadata") or {}
    user_id = metadata.get("user_id") or checkout_user_id

    if not user_id:
        return {"received": True, "ignored": True}

    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return {"received": True, "ignored": True}

    items = (sub.get("items") or {}).get("data") or []
    if not items:
        return {"received": True, "ignored": True}

    price_id = (items[0].get("price") or {}).get("id")

    configured_prices = {
        "starter": os.environ.get("STRIPE_PRICE_STARTER", "").strip(),
        "professional": os.environ.get("STRIPE_PRICE_PROFESSIONAL", "").strip(),
        "business": os.environ.get("STRIPE_PRICE_BUSINESS", "").strip(),
    }

    plan = next(
        (name for name, configured_id in configured_prices.items()
         if configured_id and configured_id == price_id),
        None
    )

    if not plan:
        return {"received": True, "ignored": True}

    status = sub.get("status", "unknown")
    customer_id = sub.get("customer")

    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    if isinstance(checkout_customer_id, dict):
        checkout_customer_id = checkout_customer_id.get("id")

    if checkout_customer_id and checkout_customer_id != customer_id:
        return {"received": True, "ignored": True}

    period_start = sub.get("current_period_start")
    period_end = sub.get("current_period_end")

    if period_start is None:
        period_start = items[0].get("current_period_start")
    if period_end is None:
        period_end = items[0].get("current_period_end")

    active = status in ("active", "trialing")

    with db.connect() as con:
        user = con.execute(
            "SELECT id FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        if not user:
            return {"received": True, "ignored": True}

        existing = con.execute(
            """
            SELECT id, user_id
            FROM subscriptions
            WHERE stripe_subscription_id = ?
            LIMIT 1
            """,
            (subscription_id,)
        ).fetchone()

        if existing and int(existing["user_id"]) != user_id:
            return {"received": True, "ignored": True}

        values = (
            customer_id,
            subscription_id,
            price_id,
            plan,
            status,
            _stripe_timestamp(period_start),
            _stripe_timestamp(period_end),
            int(bool(sub.get("cancel_at_period_end"))),
        )

        if existing:
            con.execute(
                """
                UPDATE subscriptions
                SET stripe_customer_id = ?,
                    stripe_price_id = ?,
                    plan = ?,
                    status = ?,
                    current_period_start = ?,
                    current_period_end = ?,
                    cancel_at_period_end = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE stripe_subscription_id = ?
                """,
                (
                    customer_id, price_id, plan, status,
                    _stripe_timestamp(period_start),
                    _stripe_timestamp(period_end),
                    int(bool(sub.get("cancel_at_period_end"))),
                    subscription_id,
                )
            )
        else:
            con.execute(
                """
                INSERT INTO subscriptions (
                    user_id, stripe_customer_id,
                    stripe_subscription_id, stripe_price_id,
                    plan, status, current_period_start,
                    current_period_end, cancel_at_period_end
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id,) + values
            )

        current = con.execute(
            """
            SELECT plan
            FROM subscriptions
            WHERE user_id = ?
              AND status IN ('active', 'trialing')
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()

        effective_plan = current["plan"] if current else "free"

        con.execute(
            "UPDATE users SET plan = ? WHERE id = ?",
            (effective_plan, user_id)
        )

    return {"received": True}
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
