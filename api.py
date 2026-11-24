
import csv
import io
import os
import hashlib
import secrets
from datetime import date, datetime, timedelta, timezone
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field
import jwt
import stripe
import auth
import email_service
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

import db

app = FastAPI(title="Insolvency Radar")

# Customer authentication configuration

security = HTTPBearer(auto_error=False)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    device_id: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
class PasswordResetRequest(BaseModel):
    email: EmailStr
class PasswordResetConfirmRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=10, max_length=128)
class WatchlistRequest(BaseModel):
    acn: str = Field(pattern=r"^\d{9}$")
    company_name: str | None = Field(default=None, max_length=250)
class CheckoutRequest(BaseModel):
    plan: str = Field(pattern=r"^(starter|professional|business)$")

class DeviceRegistrationRequest(BaseModel):
    device_id: str = Field(min_length=20, max_length=128)
    device_label: str | None = Field(default=None, max_length=120)

class DeviceVerificationRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)

def _subscription_plan(user_id: int) -> str:
    """Return a paid plan only when a corresponding subscription is current."""
    now = datetime.now(timezone.utc)
    with db.connect() as con:
        rows = con.execute(
            """
            SELECT plan, status, current_period_end
            FROM subscriptions
            WHERE user_id = ?
              AND status IN ('active', 'trialing')
            ORDER BY updated_at DESC
            """,
            (user_id,)
        ).fetchall()

    for row in rows:
        plan = str(row["plan"] or "").lower()
        if plan not in ("starter", "professional", "business"):
            continue
        end_value = row["current_period_end"]
        if not end_value:
            # Stripe subscriptions should carry a billing period end. Fail
            # closed rather than granting indefinite paid access.
            continue
        try:
            expiry = datetime.fromisoformat(str(end_value).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if expiry >= now:
            return plan
    return "free"


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
):
    """Verify the login token and retrieve the active customer and entitlement."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    try:
        payload = auth.decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired login token.")

    with db.connect() as con:
        user = con.execute(
            "SELECT id, email, plan, is_active FROM users WHERE id=?",
            (user_id,)
        ).fetchone()

    if user is None or not user["is_active"]:
        raise HTTPException(status_code=401, detail="Customer account is unavailable.")

    result = dict(user)
    result["plan"] = _subscription_plan(user_id)
    return result


def get_verified_device_user(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Verify registered browser for Free members and registered devices for paid members."""
    raw_device_id = request.headers.get("X-IP-Device-ID", "")

    if len(raw_device_id) < 20 or len(raw_device_id) > 128:
        raise HTTPException(
            status_code=403,
            detail="Register this browser to access company search."
        )

    device_hash = hashlib.sha256(raw_device_id.encode("utf-8")).hexdigest()

    if user.get("plan") == "free":
        with db.connect() as con:
            device = con.execute(
                """
                SELECT user_id
                FROM free_device_registry
                WHERE device_hash=?
                """,
                (device_hash,)
            ).fetchone()

            if not device or device["user_id"] != user["id"]:
                raise HTTPException(
                    status_code=403,
                    detail="This browser is not registered to your Free account."
                )

            con.execute(
                """
                UPDATE free_device_registry
                SET last_seen_at=CURRENT_TIMESTAMP
                WHERE device_hash=? AND user_id=?
                """,
                (device_hash, user["id"])
            )

        return user

    with db.connect() as con:
        device = con.execute(
            "SELECT id FROM user_devices WHERE user_id=? AND device_hash=?",
            (user["id"], device_hash)
        ).fetchone()

        if device:
            con.execute(
                "UPDATE user_devices SET last_seen_at=CURRENT_TIMESTAMP WHERE id=?",
                (device["id"],)
            )

    if not device:
        raise HTTPException(
            status_code=403,
            detail="This device is not registered for your membership."
        )

    return user



def _consume_free_search(user_id: int) -> None:
    """Atomically enforce five lifetime searches and one search per UTC day."""
    usage_day = datetime.now(timezone.utc).date().isoformat()

    with db.connect() as con:
        db.begin_write(con, user_id)

        lifetime_row = con.execute(
            "SELECT COALESCE(SUM(search_count), 0) AS total "
            "FROM search_usage WHERE user_id=?",
            (user_id,)
        ).fetchone()

        lifetime_count = int(lifetime_row["total"] or 0)

        if lifetime_count >= 5:
            raise HTTPException(
                status_code=429,
                detail="Your five free searches have been used. Upgrade for continued access."
            )

        row = con.execute(
            "SELECT search_count FROM search_usage "
            "WHERE user_id=? AND usage_date=?",
            (user_id, usage_day)
        ).fetchone()

        if row and int(row["search_count"]) >= 1:
            raise HTTPException(
                status_code=429,
                detail="Your free search has been used for today. Upgrade for full access."
            )

        if row:
            con.execute(
                "UPDATE search_usage SET search_count=search_count+1, "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE user_id=? AND usage_date=?",
                (user_id, usage_day)
            )
        else:
            con.execute(
                "INSERT INTO search_usage "
                "(user_id, usage_date, search_count) VALUES (?, ?, 1)",
                (user_id, usage_day)
            )



def _free_preview(items):
    """Return only fields approved for the limited free preview."""
    allowed = {
        "id", "kind", "status", "entity_name", "state", "suburb",
        "postcode", "notice_date", "deadline"
    }
    return [{key: value for key, value in item.items() if key in allowed} for item in items]


def _require_paid_member(user: dict) -> None:
    if user.get("plan") not in ("starter", "professional", "business"):
        raise HTTPException(status_code=403, detail="An active paid subscription is required.")


def _require_admin_refresh(request: Request) -> None:
    expected = os.environ.get("RADAR_ADMIN_KEY", "").strip()
    supplied = request.headers.get("X-Radar-Admin-Key", "")
    if not expected or len(expected) < 32 or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Administrator authorization required.")


def _require_wp_bridge(request: Request) -> None:
    """Allow customer credential operations only through the trusted WP bridge."""
    expected = os.environ.get("IP_WP_BRIDGE_SECRET", "").strip()
    supplied = request.headers.get("X-IP-Bridge-Key", "")
    if not expected or len(expected) < 32:
        raise HTTPException(status_code=503, detail="WordPress membership bridge is not configured.")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Use the Insolvency Pulse member portal to sign in.")

@app.post("/api/auth/register")
def register_customer(data: RegisterRequest, request: Request):
    """Register a new customer account through the WordPress portal."""
    _require_wp_bridge(request)

    email = str(data.email).strip().lower()
    device_id = (data.device_id or "").strip()

    if not device_id:
        raise HTTPException(
            status_code=400,
            detail="Browser registration identifier is required. Please refresh the page and try again."
        )

    device_hash = hashlib.sha256(device_id.encode("utf-8")).hexdigest()

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

            con.execute(
                """
                INSERT INTO free_device_registry (
                    user_id, device_hash, device_label
                )
                VALUES (?, ?, ?)
                """,
                (user_id, device_hash, "Free member browser")
            )

    except db.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="An account with this email or browser is already registered."
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
def login_customer(data: LoginRequest, request: Request):
    """Authenticate a customer through the WordPress portal."""
    _require_wp_bridge(request)

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
@app.post("/api/auth/password-reset/request")
def request_password_reset(data: PasswordResetRequest):
    """Request a password reset without revealing whether an account exists."""

    email = str(data.email).strip().lower()

    with db.connect() as con:
        user = con.execute(
            """
            SELECT id, email
            FROM users
            WHERE LOWER(email)=LOWER(?) AND is_active=1
            """,
            (email,)
        ).fetchone()

    if user is not None:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(minutes=30)).isoformat()

        with db.connect() as con:
            con.execute(
                """
                UPDATE password_reset_tokens
                SET used_at=?
                WHERE user_id=? AND used_at IS NULL
                """,
                (now.isoformat(), user["id"])
            )
            con.execute(
                """
                INSERT INTO password_reset_tokens
                (user_id, token_hash, expires_at)
                VALUES (?, ?, ?)
                """,
                (user["id"], token_hash, expires)
            )

        try:
            email_service.send_password_reset_email(user["email"], token)
        except Exception as exc:
            with db.connect() as con:
                con.execute(
                    """
                    UPDATE password_reset_tokens
                    SET used_at=?
                    WHERE token_hash=? AND used_at IS NULL
                    """,
                    (datetime.now(timezone.utc).isoformat(), token_hash)
                )
            print("Password reset email delivery failed:", str(exc))

    return {
        "message": "If an active account exists, password reset instructions will be sent."
    }


@app.post("/api/auth/password-reset/confirm")
def confirm_password_reset(data: PasswordResetConfirmRequest):
    """Set a new password using a valid, unused reset token."""

    token_hash = hashlib.sha256(
        data.token.encode("utf-8")
    ).hexdigest()

    now = datetime.now(timezone.utc)

    with db.connect() as con:
        record = con.execute(
            """
            SELECT t.id, t.user_id, t.expires_at, t.used_at
            FROM password_reset_tokens t
            JOIN users u ON u.id=t.user_id
            WHERE t.token_hash=? AND u.is_active=1
            """,
            (token_hash,)
        ).fetchone()

        if record is None or record["used_at"] is not None:
            raise HTTPException(
                status_code=400,
                detail="Invalid or expired password reset link."
            )

        try:
            expiry = datetime.fromisoformat(record["expires_at"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="Invalid or expired password reset link."
            )

        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        if expiry <= now:
            raise HTTPException(
                status_code=400,
                detail="Invalid or expired password reset link."
            )

        password_hash = auth.hash_password(data.new_password)

        con.execute(
            """
            UPDATE users
            SET password_hash=?, updated_at=?
            WHERE id=?
            """,
            (password_hash, now.isoformat(), record["user_id"])
        )

        con.execute(
            """
            UPDATE password_reset_tokens
            SET used_at=?
            WHERE user_id=? AND used_at IS NULL
            """,
            (now.isoformat(), record["user_id"])
        )

    return {"message": "Your password has been reset successfully. Please log in."}

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

    except stripe.error.StripeError as exc:
        print(
            "STRIPE CHECKOUT ERROR:",
            type(exc).__name__,
            "message:", str(exc),
            "code:", getattr(exc, "code", None),
            "param:", getattr(exc, "param", None),
            "request_id:", getattr(exc, "request_id", None),
            flush=True
        )
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

    event_data = event._to_dict_recursive()
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
        sub = subscription._to_dict_recursive()

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
@app.post("/api/devices/register")
def register_device(
    data: DeviceRegistrationRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Register a first device per category or email-verify a replacement."""
    _require_wp_bridge(request)
   if user["plan"] == "free":
    device_id = (data.device_id or "").strip()

    if not device_id:
        raise HTTPException(
            status_code=400,
            detail="Browser device identifier is missing. Please refresh and sign in again."
        )

    device_hash = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
    device_label = (data.device_label or "Free member browser").strip()[:120]

    try:
        with db.connect() as con:
            db.begin_write(con, user["id"])

            existing = con.execute(
                "SELECT user_id FROM free_device_registry WHERE device_hash=?",
                (device_hash,)
            ).fetchone()

            if existing and existing["user_id"] != user["id"]:
                raise HTTPException(
                    status_code=409,
                    detail="This browser is already registered to another Free account."
                )

            if existing:
                con.execute(
                    """
                    UPDATE free_device_registry
                    SET last_seen_at=CURRENT_TIMESTAMP
                    WHERE device_hash=? AND user_id=?
                    """,
                    (device_hash, user["id"])
                )
            else:
                con.execute(
                    """
                    INSERT INTO free_device_registry
                    (user_id, device_hash, device_label)
                    VALUES (?, ?, ?)
                    """,
                    (user["id"], device_hash, device_label)
                )

    except db.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="This browser is already registered to another Free account."
        )

    return {"status": "registered", "device_type": "free"}

    user_agent = request.headers.get("X-IP-Device-User-Agent", request.headers.get("User-Agent", "")).lower()
    device_type = "mobile" if any(term in user_agent for term in ("iphone", "ipad", "android", "mobile")) else "desktop"
    device_hash = hashlib.sha256(data.device_id.encode("utf-8")).hexdigest()
    device_label = (data.device_label or ("Mobile device" if device_type == "mobile" else "Desktop/laptop")).strip()[:120]

    with db.connect() as con:
        db.begin_write(con, user["id"])
        current = con.execute(
            "SELECT id, device_hash FROM user_devices WHERE user_id=? AND device_type=?",
            (user["id"], device_type)
        ).fetchone()
        if current and secrets.compare_digest(current["device_hash"], device_hash):
            con.execute("UPDATE user_devices SET last_seen_at=CURRENT_TIMESTAMP WHERE id=?", (current["id"],))
            return {"status": "registered", "device_type": device_type}
        if not current:
            con.execute(
                "INSERT INTO user_devices (user_id, device_type, device_hash, device_label) VALUES (?, ?, ?, ?)",
                (user["id"], device_type, device_hash, device_label)
            )
            return {"status": "registered", "device_type": device_type}

        last_change = con.execute(
            "SELECT changed_at FROM device_change_history WHERE user_id=? ORDER BY changed_at DESC LIMIT 1",
            (user["id"],)
        ).fetchone()
        if last_change:
            try:
                last_changed_at = datetime.fromisoformat(str(last_change["changed_at"]).replace("Z", "+00:00"))
                if last_changed_at.tzinfo is None:
                    last_changed_at = last_changed_at.replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(status_code=503, detail="Device change history requires administrator review.")
            if datetime.now(timezone.utc) - last_changed_at < timedelta(days=30):
                raise HTTPException(status_code=429, detail="A device can only be replaced once every 30 days.")

        # Invalidate any older pending verification for this device category.
        con.execute(
            "UPDATE device_change_tokens SET used_at=CURRENT_TIMESTAMP WHERE user_id=? AND device_type=? AND used_at IS NULL",
            (user["id"], device_type)
        )
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        con.execute(
            "INSERT INTO device_change_tokens (user_id, device_type, device_hash, device_label, token_hash, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], device_type, device_hash, device_label, token_hash, expires)
        )

    try:
        email_service.send_device_change_verification_email(user["email"], raw_token, device_type, device_label)
    except Exception as exc:
        print("DEVICE VERIFICATION EMAIL FAILED:", type(exc).__name__, flush=True)
        raise HTTPException(status_code=503, detail="We could not send the device verification email. Please try again later.")
    return {"status": "verification_required", "device_type": device_type,
            "message": "We sent a device replacement verification link to your registered email address."}


@app.post("/api/devices/verify")
def verify_device_change(data: DeviceVerificationRequest, request: Request):
    _require_wp_bridge(request)
    token_hash = hashlib.sha256(data.token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    with db.connect() as con:
        record = con.execute(
            "SELECT id, user_id, device_type, device_hash, device_label, expires_at, used_at FROM device_change_tokens WHERE token_hash=?",
            (token_hash,)
        ).fetchone()
        if not record or record["used_at"] is not None:
            raise HTTPException(status_code=400, detail="This device verification link is invalid or has already been used.")
        try:
            expires = datetime.fromisoformat(str(record["expires_at"]).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="This device verification link is invalid or expired.")
        if expires < now:
            raise HTTPException(status_code=400, detail="This device verification link has expired. Please sign in and request a new one.")

        db.begin_write(con, record["user_id"])
        last_change = con.execute(
            "SELECT changed_at FROM device_change_history WHERE user_id=? ORDER BY changed_at DESC LIMIT 1",
            (record["user_id"],)
        ).fetchone()
        if last_change:
            last_changed_at = datetime.fromisoformat(str(last_change["changed_at"]).replace("Z", "+00:00"))
            if last_changed_at.tzinfo is None:
                last_changed_at = last_changed_at.replace(tzinfo=timezone.utc)
            if now - last_changed_at < timedelta(days=30):
                raise HTTPException(status_code=429, detail="A device can only be replaced once every 30 days.")

        con.execute(
            "UPDATE user_devices SET device_hash=?, device_label=?, registered_at=CURRENT_TIMESTAMP, last_seen_at=CURRENT_TIMESTAMP WHERE user_id=? AND device_type=?",
            (record["device_hash"], record["device_label"], record["user_id"], record["device_type"])
        )
        con.execute(
            "INSERT INTO device_change_history (user_id, device_type) VALUES (?, ?)",
            (record["user_id"], record["device_type"])
        )
        con.execute("UPDATE device_change_tokens SET used_at=? WHERE id=?", (now.isoformat(), record["id"]))
    return {"status": "verified", "message": "Your device replacement is approved. Please sign in again on the new device."}


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
    try:
        with db.connect() as con:
            con.execute("SELECT 1").fetchone()
            database = con.dialect_name

        return {
            "status": "ok",
            "application": "Insolvencys Radar",
            "database": "connected",
            "database_type": database
        }

    except Exception as exc:
        print("DATABASE HEALTH CHECK FAILED:", type(exc).__name__)
        return {
            "status": "error",
            "application": "Insolvencys Radar",
            "database": "disconnected"
        }

@app.get("/api/leads")
def leads(
    kind: str | None = None,
    state: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=20000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_verified_device_user),
):
    is_free = user["plan"] == "free"
    if is_free:
        _consume_free_search(user["id"])
        limit, offset = 5, 0
    with db.connect() as con:
        rows = db.search(
            con, kind=kind, state=state, status=status, q=q,
            limit=limit, offset=offset
        )
    return _free_preview(rows) if is_free else rows


@app.get("/api/leads/page")
def leads_page(
    kind: str | None = None,
    state: str | None = None,
    status: str | None = None,
    category: str | None = None,
    dataset: str | None = None,
    days: int | None = Query(None, ge=1, le=3650),
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_verified_device_user),
):
    from datetime import date, timedelta
    categories = {
        "L": ("insolvency", "liquidation"),
        "A": ("insolvency", "administration"),
        "R": ("insolvency", "receivership"),
        "E": ("asic_company", "external_administration"),
        "S": ("asic_company", "strike_off"),
    }
    if category:
        if category not in categories:
            raise HTTPException(status_code=400, detail="Invalid category")
        kind, status = categories[category]
    if dataset not in (None, "", "current", "historical"):
        raise HTTPException(status_code=400, detail="Invalid dataset")

    is_free = user["plan"] == "free"
    if is_free:
        _consume_free_search(user["id"])
        limit, offset = 5, 0

    since = (date.today() - timedelta(days=days)).isoformat() if days else None
    with db.connect() as con:
        total = db.count_search(con, kind=kind, state=state, status=status, q=q, since=since, dataset=dataset or None)
        items = db.search(
            con, kind=kind, state=state, status=status, q=q,
            limit=limit, offset=offset, since=since, dataset=dataset or None
        )
    if is_free:
        items = _free_preview(items)
        total = min(total, 5)
    return {"items": items, "total": total, "limit": limit, "offset": offset,
            "has_more": bool(is_free and total >= 5)}


@app.get("/api/status")
def status(request: Request):
    _require_admin_refresh(request)
    return _last


@app.get("/api/export.csv")
def export(
    kind: str | None = None,
    state: str | None = None,
    status: str | None = None,
    q: str | None = None,
    dataset: str | None = None,
    user: dict = Depends(get_verified_device_user),
):
    _require_paid_member(user)
    if dataset not in (None, "", "current", "historical"):
        raise HTTPException(status_code=400, detail="Invalid dataset")

    cols = [c for c in db.COLUMNS if c != "raw_text"]

    def csv_rows():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        yield buf.getvalue()
        offset = 0
        batch_size = 5000
        with db.connect() as con:
            while True:
                rows = db.search(
                    con, kind=kind, state=state, status=status, q=q,
                    limit=batch_size, offset=offset, dataset=dataset or None
                )
                if not rows:
                    break
                buf.seek(0)
                buf.truncate(0)
                writer.writerows(rows)
                yield buf.getvalue()
                offset += len(rows)
                if len(rows) < batch_size:
                    break

    return StreamingResponse(
        csv_rows(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="insolvencys-radar.csv"'}
    )

@app.post("/api/refresh")
def refresh(request: Request):
    _require_admin_refresh(request)
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
