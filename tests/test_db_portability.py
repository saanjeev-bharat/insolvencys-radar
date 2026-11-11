import pytest

import db


def test_sqlite_schema_and_row_compatibility(tmp_path):
    database = tmp_path / "portable.sqlite3"
    with db.connect(str(database)) as con:
        con.execute(
            "INSERT INTO users (email, password_hash, plan) VALUES (?, ?, ?)",
            ("test@example.com", "hash", "free"),
        )
        user = con.execute(
            "SELECT id, email, plan, is_active FROM users WHERE email=?",
            ("test@example.com",),
        ).fetchone()
        assert user[0] == user["id"]
        assert dict(user)["email"] == "test@example.com"


def test_sqlite_watchlist_write_lock_and_persistence(tmp_path):
    database = tmp_path / "watchlist.sqlite3"
    with db.connect(str(database)) as con:
        con.execute(
            "INSERT INTO users (email, password_hash, plan) VALUES (?, ?, ?)",
            ("lock@example.com", "hash", "free"),
        )
    with db.connect(str(database)) as con:
        db.begin_write(con, 1)
        con.execute(
            "INSERT INTO watchlists (user_id, acn, company_name) VALUES (?, ?, ?)",
            (1, "123456789", "Example Pty Ltd"),
        )
    with db.connect(str(database)) as con:
        assert con.execute("SELECT COUNT(*) FROM watchlists").fetchone()[0] == 1


def test_email_is_case_insensitive_unique(tmp_path):
    database = tmp_path / "email.sqlite3"
    with db.connect(str(database)) as con:
        con.execute(
            "INSERT INTO users (email, password_hash, plan) VALUES (?, ?, ?)",
            ("test@example.com", "hash", "free"),
        )
    with pytest.raises(db.IntegrityError):
        with db.connect(str(database)) as con:
            con.execute(
                "INSERT INTO users (email, password_hash, plan) VALUES (?, ?, ?)",
                ("TEST@example.com", "hash", "free"),
            )


def test_postgresql_schema_ddl_compiles():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    dialect = postgresql.dialect()
    for table in db.metadata.sorted_tables:
        str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            str(CreateIndex(index).compile(dialect=dialect))


def test_postgres_url_normalisation(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@example.test/db")
    assert db._database_url().startswith("postgresql+psycopg://")


def test_search_is_case_insensitive(tmp_path):
    database = tmp_path / "search.sqlite3"
    with db.connect(str(database)) as con:
        db.upsert(con, {
            "id": "lead-1", "kind": "insolvency", "source": "test",
            "entity_name": "Harbour City Builders", "asset_summary": "Construction",
        })
        rows = db.search(con, q="HARBOUR CITY")
        assert len(rows) == 1
        assert rows[0]["entity_name"] == "Harbour City Builders"
