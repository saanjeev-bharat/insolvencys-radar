import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_SECRET_KEY", "local-test-secret-key-12345678901234567890")
    monkeypatch.setenv("IP_WP_BRIDGE_SECRET", "local-test-wordpress-bridge-secret-1234567890")

    import db
    import api

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite3"))

    with db.connect():
        pass

    with TestClient(api.app) as test_client:
        yield test_client


def create_customer(client):
    response = client.post(
        "/api/auth/register",
        headers={
            "X-IP-Bridge-Key": "local-test-wordpress-bridge-secret-1234567890"
        },
        json={
            "email": "customer@example.com",
            "password": "Test-Password-2026",
            "device_id": "free-device-identifier-0000001"
        }
    )
    assert response.status_code == 200
    return response.json()["token"]


def test_customer_registration_and_login(client):
    token = create_customer(client)

    headers = {"Authorization": "Bearer " + token}
    response = client.get("/api/auth/me", headers=headers)

    assert response.status_code == 200
    assert response.json()["user"]["plan"] == "free"

    duplicate = client.post(
        "/api/auth/register",
        headers={"X-IP-Bridge-Key": "local-test-wordpress-bridge-secret-1234567890"},
        json={
            "email": "customer@example.com",
            "password": "Test-Password-2026"
        }
    )
    assert duplicate.status_code == 409

    login = client.post(
        "/api/auth/login",
        headers={"X-IP-Bridge-Key": "local-test-wordpress-bridge-secret-1234567890"},
        json={
            "email": "customer@example.com",
            "password": "Test-Password-2026"
        }
    )
    assert login.status_code == 200


def test_watchlist_add_view_and_remove(client):
    token = create_customer(client)
    headers = {"Authorization": "Bearer " + token}

    add = client.post(
        "/api/watchlist",
        headers=headers,
        json={"acn": "123456789", "company_name": "Test Company"}
    )
    assert add.status_code == 200

    view = client.get("/api/watchlist", headers=headers)
    assert view.status_code == 200
    assert len(view.json()["items"]) == 1

    remove = client.delete("/api/watchlist/123456789", headers=headers)
    assert remove.status_code == 200

    view_again = client.get("/api/watchlist", headers=headers)
    assert len(view_again.json()["items"]) == 0


def test_watchlist_free_plan_limit(client):
    token = create_customer(client)
    headers = {"Authorization": "Bearer " + token}

    for number in range(5):
        response = client.post(
            "/api/watchlist",
            headers=headers,
            json={
                "acn": str(123456789 + number * 100000000),
                "company_name": "Test Company"
            }
        )
        assert response.status_code == 200

    sixth = client.post(
        "/api/watchlist",
        headers=headers,
        json={"acn": "623456789", "company_name": "Sixth Company"}
    )
    assert sixth.status_code == 403


def test_duplicate_watchlist_company(client):
    token = create_customer(client)
    headers = {"Authorization": "Bearer " + token}

    data = {"acn": "123456789", "company_name": "Test Company"}

    first = client.post("/api/watchlist", headers=headers, json=data)
    second = client.post("/api/watchlist", headers=headers, json=data)

    assert first.status_code == 200
    assert second.status_code == 200

    view = client.get("/api/watchlist", headers=headers)
    assert len(view.json()["items"]) == 1


def test_watchlist_requires_login_and_valid_acn(client):
    unauthorised = client.get("/api/watchlist")
    assert unauthorised.status_code == 401

    token = create_customer(client)
    headers = {"Authorization": "Bearer " + token}

    invalid = client.post(
        "/api/watchlist",
        headers=headers,
        json={"acn": "12345", "company_name": "Invalid Company"}
    )
    assert invalid.status_code == 422

    invalid_delete = client.delete(
        "/api/watchlist/12345",
        headers=headers
    )
    assert invalid_delete.status_code == 400

def _insert_test_lead():
    import db
    with db.connect() as con:
        db.upsert(con, {
            "id": "free-preview-1", "kind": "insolvency",
            "status": "liquidation", "entity_name": "Preview Pty Ltd",
            "acn": "123456789", "abn": "51123456789", "state": "NSW",
            "suburb": "Sydney", "postcode": "2000",
            "contact_name": "Private Contact", "contact_phone": "0400000000",
            "contact_email": "private@example.com", "source": "test",
            "source_url": "https://example.com/source", "asset_summary": "Private details",
        })


def test_company_search_requires_authentication(client):
    assert client.get("/api/leads/page").status_code == 401
    assert client.get("/api/leads").status_code == 401


def test_free_member_gets_five_field_redacted_preview_and_one_search_daily(client):
    token = create_customer(client)
    headers = {"Authorization": "Bearer " + token}
    _insert_test_lead()

    first = client.get("/api/leads/page", headers=headers, params={"q": "Preview"})
    assert first.status_code == 200
    payload = first.json()
    assert payload["limit"] == 5
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["entity_name"] == "Preview Pty Ltd"
    for hidden in ("acn", "abn", "contact_name", "contact_phone", "contact_email", "source_url", "asset_summary"):
        assert hidden not in item

    second = client.get("/api/leads/page", headers=headers, params={"q": "different"})
    assert second.status_code == 429

    bypass = client.get("/api/leads", headers=headers, params={"limit": 20000})
    assert bypass.status_code == 429


def test_free_member_cannot_export_csv(client):
    token = create_customer(client)
    response = client.get("/api/export.csv", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 403


def test_refresh_requires_admin_key(client, monkeypatch):
    monkeypatch.setenv("RADAR_ADMIN_KEY", "test-admin-key-that-is-at-least-32-characters-long")
    assert client.post("/api/refresh").status_code == 403


def test_paid_member_access_requires_current_subscription(client):
    token = create_customer(client)
    headers = {"Authorization": "Bearer " + token}
    import db
    from datetime import datetime, timedelta, timezone
    expiry = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connect() as con:
        con.execute(
            """INSERT INTO subscriptions
               (user_id, plan, status, current_period_end)
               VALUES (?, 'professional', 'active', ?)""",
            (1, expiry),
        )
    device = client.post("/api/devices/register", headers={
        **headers, "X-IP-Bridge-Key": "local-test-wordpress-bridge-secret-1234567890", "X-IP-Device-User-Agent": "Mozilla/5.0 Windows NT 10.0"
    }, json={"device_id": "paid-export-device-identifier-0001"})
    assert device.status_code == 200
    response = client.get("/api/export.csv", headers={
        **headers, "X-IP-Device-ID": "paid-export-device-identifier-0001"
    })
    assert response.status_code == 200


def test_paid_device_registration_and_email_verified_replacement(client, monkeypatch):
    import api
    import db
    from datetime import datetime, timedelta, timezone

    token = create_customer(client)
    headers = {"Authorization": "Bearer " + token}
    expiry = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connect() as con:
        con.execute(
            "INSERT INTO subscriptions (user_id, plan, status, current_period_end) VALUES (?, 'starter', 'active', ?)",
            (1, expiry),
        )

    sent = {}
    def capture_email(email, raw_token, device_type, label=None):
        sent.update({"email": email, "token": raw_token, "device_type": device_type})
    monkeypatch.setattr(api.email_service, "send_device_change_verification_email", capture_email)

    desktop_headers = {**headers, "X-IP-Bridge-Key": "local-test-wordpress-bridge-secret-1234567890", "X-IP-Device-User-Agent": "Mozilla/5.0 Windows NT 10.0"}
    first = client.post("/api/devices/register", headers=desktop_headers,
                        json={"device_id": "desktop-device-identifier-0001"})
    assert first.status_code == 200
    assert first.json()["status"] == "registered"

    replacement = client.post("/api/devices/register", headers=desktop_headers,
                              json={"device_id": "desktop-device-identifier-0002"})
    assert replacement.status_code == 200
    assert replacement.json()["status"] == "verification_required"
    assert sent["email"] == "customer@example.com"
    assert sent["device_type"] == "desktop"

    verified = client.post("/api/devices/verify", headers={"X-IP-Bridge-Key": "local-test-wordpress-bridge-secret-1234567890"}, json={"token": sent["token"]})
    assert verified.status_code == 200
    assert verified.json()["status"] == "verified"

    _insert_test_lead()
    old_device = client.get("/api/leads/page", headers={
        **headers, "X-IP-Device-ID": "desktop-device-identifier-0001"
    })
    assert old_device.status_code == 403
    new_device = client.get("/api/leads/page", headers={
        **headers, "X-IP-Device-ID": "desktop-device-identifier-0002"
    })
    assert new_device.status_code == 200

    # The separate mobile slot is available without consuming a replacement.
    mobile = client.post("/api/devices/register", headers={
        **headers, "X-IP-Bridge-Key": "local-test-wordpress-bridge-secret-1234567890",
        "X-IP-Device-User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS) Mobile"
    }, json={"device_id": "mobile-device-identifier-0000001"})
    assert mobile.status_code == 200
    assert mobile.json()["status"] == "registered"

    # A replacement within 30 days is blocked, even after email verification.
    too_soon = client.post("/api/devices/register", headers=desktop_headers,
                           json={"device_id": "desktop-device-identifier-0003"})
    assert too_soon.status_code == 429


def test_free_member_device_registration(client):
    token = create_customer(client)

    response = client.post(
        "/api/devices/register",
        headers={
            "Authorization": "Bearer " + token,
            "X-IP-Bridge-Key": "local-test-wordpress-bridge-secret-1234567890"
        },
        json={"device_id": "free-device-identifier-0000001"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "registered"


def test_free_search_returns_at_most_five_records(client):
    token = create_customer(client)
    headers = {"Authorization": "Bearer " + token}
    import db
    with db.connect() as con:
        for number in range(7):
            db.upsert(con, {
                "id": f"free-batch-{number}", "kind": "insolvency",
                "status": "liquidation", "entity_name": f"Preview Company {number}",
                "source": "test",
            })
    response = client.get("/api/leads/page", headers=headers)
    assert response.status_code == 200
    assert response.json()["limit"] == 5
    assert len(response.json()["items"]) == 5
    assert response.json()["total"] == 5


def test_expired_paid_subscription_falls_back_to_free(client):
    token = create_customer(client)
    headers = {"Authorization": "Bearer " + token}
    import db
    from datetime import datetime, timedelta, timezone
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connect() as con:
        con.execute(
            "INSERT INTO subscriptions (user_id, plan, status, current_period_end) VALUES (?, 'business', 'active', ?)",
            (1, expired),
        )
    response = client.get("/api/export.csv", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "An active paid subscription is required."


def test_direct_customer_login_is_blocked_without_wordpress_bridge(client):
    response = client.post("/api/auth/register", json={
        "email": "direct@example.com", "password": "Test-Password-2026"
    })
    assert response.status_code == 403


def test_refresh_status_requires_admin_key(client, monkeypatch):
    monkeypatch.setenv("RADAR_ADMIN_KEY", "test-admin-key-that-is-at-least-32-characters-long")
    assert client.get("/api/status").status_code == 403
    assert client.get("/api/status", headers={
        "X-Radar-Admin-Key": "test-admin-key-that-is-at-least-32-characters-long"
    }).status_code == 200
