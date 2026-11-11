import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_SECRET_KEY", "local-test-secret-key-12345678901234567890")

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
        json={
            "email": "customer@example.com",
            "password": "Test-Password-2026"
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
        json={
            "email": "customer@example.com",
            "password": "Test-Password-2026"
        }
    )
    assert duplicate.status_code == 409

    login = client.post(
        "/api/auth/login",
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