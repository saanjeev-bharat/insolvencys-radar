
from pathlib import Path

import db
import pipeline
import sources
from extract import extract_regex


HERE = Path(__file__).resolve().parent
SAMPLE = HERE / "sample_notices.txt"


def sample_blocks():
    """Read sample notices, supporting Windows and Unix line endings."""

    text = SAMPLE.read_text(encoding="utf-8")
    return [
        block.strip()
        for block in text.split("\n\n")
        if block.strip()
    ]


def test_extract_liquidation():
    blocks = sample_blocks()
    assert blocks, "Sample notices file is empty"

    lead = extract_regex(blocks[0])

    assert lead["status"] == "liquidation"
    assert lead["acn"] == "123456789"
    assert lead["abn"] == "12345678901"
    assert lead["state"] == "NSW"
    assert lead["postcode"] == "2150"
    assert lead["contact_phone"] == "02 9555 1234"
    assert lead["contact_email"] == "jsmith@apexrestructuring.com.au"
    assert lead["entity_name"].lower().startswith("harbour city builders")


def test_pipeline_filters_noise(tmp_path):
    path = str(tmp_path / "t.db")

    count = pipeline.run(
        [sources.FileSource(str(SAMPLE))],
        db_path=path,
    )

    assert count == 2, "The garden-party text should be filtered out"

    with db.connect(path) as con:
        rows = db.search(con, state="WA")

    assert len(rows) == 1
    assert rows[0]["status"] == "administration"
    assert rows[0]["contact_email"] == "jc@citizenbr.com.au"


def test_practitioner_names():
    blocks = sample_blocks()

    assert len(blocks) >= 2

    assert extract_regex(blocks[0])["contact_name"] == "Jane Smith"
    assert extract_regex(blocks[1])["contact_name"] == "John Citizen"


def test_invalid_calendar_date_is_ignored():
    lead = extract_regex("Notice dated 32 January 2026")

    assert "notice_date" not in lead


def test_auction_is_classified():
    lead = extract_regex("Public auction of company assets")

    assert lead["status"] == "auction"


def test_asic_appointment_history_is_complete_and_idempotent(tmp_path, monkeypatch):
    appointments = [
        {
            "acn": "123456789", "name": "EXAMPLE PTY LTD",
            "appointee": "JANE SMITH", "atype": "Voluntary administration",
            "cat": "A", "date": "2026-01-10", "industry": "Construction",
            "state": "NSW", "area": "Sydney", "postcode": "2000",
        },
        {
            "acn": "123456789", "name": "EXAMPLE PTY LTD",
            "appointee": "JOHN CITIZEN", "atype": "Receivership",
            "cat": "R", "date": "2026-02-15", "industry": "Construction",
            "state": "NSW", "area": "Sydney", "postcode": "2000",
        },
        {
            "acn": "123456789", "name": "EXAMPLE PTY LTD",
            "appointee": "MOHAMMAD NAJJAR",
            "atype": "Creditors' voluntary liquidation",
            "cat": "L", "date": "2026-03-20", "industry": "Construction",
            "state": "NSW", "area": "Sydney", "postcode": "2000",
        },
    ]
    monkeypatch.setattr(
        pipeline.asic_stats,
        "load_all",
        lambda months=12: (appointments, {"file": "https://example.test/asic.xlsx"}),
    )
    path = str(tmp_path / "appointment-history.db")

    with db.connect(path) as con:
        first_summary_count = pipeline.import_asic_stats(con)
        first_events = con.execute(
            "SELECT event_id, appointment_type, effective_date "
            "FROM asic_appointments WHERE acn=? ORDER BY effective_date",
            ["123456789"],
        ).fetchall()
        assert first_summary_count == 1
        assert len(first_events) == 3

        second_summary_count = pipeline.import_asic_stats(con)
        second_events = con.execute(
            "SELECT event_id FROM asic_appointments WHERE acn=?",
            ["123456789"],
        ).fetchall()
        summary = con.execute(
            "SELECT status, notice_date, raw_text FROM leads WHERE acn=?",
            ["123456789"],
        ).fetchone()

    assert second_summary_count == 1
    assert len(second_events) == 3
    assert summary["status"] == "liquidation"
    assert summary["notice_date"] == "2026-03-20"
    assert "Appointments in period: 3" in summary["raw_text"]

def test_asic_load_all_months_none_disables_date_filter(monkeypatch, tmp_path):
    captured = {}

    def fake_fetch(path):
        return "https://example.test/asic.xlsx"

    def fake_read(path, since=None):
        captured["since"] = since
        return (
            [{"acn": "123456789", "date": "2000-01-01"}],
            {"sheet": "Data set", "columns": ["ACN"]},
        )

    monkeypatch.setattr(pipeline.asic_stats, "fetch_workbook", fake_fetch)
    monkeypatch.setattr(pipeline.asic_stats, "read_appointments", fake_read)

    appointments, info = pipeline.asic_stats.load_all(
        months=None, workdir=str(tmp_path)
    )

    assert captured["since"] is None
    assert len(appointments) == 1
    assert appointments[0]["date"] == "2000-01-01"
    assert info["since"] is None
    assert info["rows"] == 1


def test_asic_load_all_default_keeps_12_month_filter(monkeypatch, tmp_path):
    import datetime

    captured = {}

    def fake_fetch(path):
        return "https://example.test/asic.xlsx"

    def fake_read(path, since=None):
        captured["since"] = since
        return ([], {"sheet": "Data set", "columns": ["ACN"]})

    monkeypatch.setattr(pipeline.asic_stats, "fetch_workbook", fake_fetch)
    monkeypatch.setattr(pipeline.asic_stats, "read_appointments", fake_read)

    pipeline.asic_stats.load_all(workdir=str(tmp_path))

    expected = (
        datetime.date.today()
        - datetime.timedelta(days=int(12 * 30.5))
    ).isoformat()
    assert captured["since"] == expected
