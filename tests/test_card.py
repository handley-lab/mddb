import pytest
import yaml as pyyaml

from mddb.card import Card, slugify


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


def test_missing_opening_delimiter():
    with pytest.raises(ValueError):
        Card.from_text("id: x\n---\nbody\n")


def test_slugify_basic():
    assert slugify("Fridge Inventory") == "fridge-inventory"


def test_slugify_collapses_punctuation():
    assert slugify("Will's GTD: Notes (2026)") == "will-s-gtd-notes-2026"


def test_slugify_empty_becomes_untitled():
    assert slugify("") == "untitled"
    assert slugify("---") == "untitled"
