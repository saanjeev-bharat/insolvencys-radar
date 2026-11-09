
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
