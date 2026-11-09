import os

import db
import pipeline
import sources
from extract import extract_regex

HERE = os.path.dirname(__file__)
SAMPLE = os.path.join(HERE, "sample_notices.txt")


def test_extract_liquidation():
    text = open(SAMPLE).read().split("\n\n")[0]
    lead = extract_regex(text)
    assert lead["status"] == "liquidation"
    assert lead["acn"] == "123456789"
    assert lead["abn"] == "12345678901"
    assert lead["state"] == "NSW" and lead["postcode"] == "2150"
    assert lead["contact_phone"] == "02 9555 1234"
    assert lead["contact_email"] == "jsmith@apexrestructuring.com.au"
    assert lead["entity_name"].lower().startswith("harbour city builders")


def test_pipeline_filters_noise(tmp_path):
    path = str(tmp_path / "t.db")
    n = pipeline.run([sources.FileSource(SAMPLE)], db_path=path)
    assert n == 2  # the garden-party text is dropped
    with db.connect(path) as con:
        rows = db.search(con, state="WA")
        assert len(rows) == 1 and rows[0]["status"] == "administration"
        assert rows[0]["contact_email"] == "jc@citizenbr.com.au"


def test_practitioner_names():
    a, b, _ = open(SAMPLE).read().split("\n\n")
    assert extract_regex(a)["contact_name"] == "Jane Smith"
    assert extract_regex(b)["contact_name"] == "John Citizen"
