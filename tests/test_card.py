import yaml as pyyaml

from mddb.card import Card


def test_dates_parse_as_dates():
    data = pyyaml.safe_load("d: 2026-06-02\n")
    assert data["d"].isoformat() == "2026-06-02"


def test_strings_stay_strings():
    data = pyyaml.safe_load('d: "2026-06-02"\n')
    assert data == {"d": "2026-06-02"}


def test_roundtrip():
    c = Card(yaml={"id": "x", "tags": ["a", "b"]}, body="hello\n")
    parsed = Card.from_text(str(c))
    assert parsed.yaml == {"id": "x", "tags": ["a", "b"]}
    assert parsed.body == "hello\n"
