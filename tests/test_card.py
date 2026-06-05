from pathlib import Path

import pytest
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


def test_card_tags_returns_list_when_present():
    card = Card(yaml={"id": "x", "tags": ["area/work", "topic/cosmology"]})
    assert card.tags == ["area/work", "topic/cosmology"]


def test_card_tags_raises_keyerror_when_absent():
    card = Card(yaml={"id": "x"})
    with pytest.raises(KeyError):
        _ = card.tags


def test_card_tags_returns_mutable_backing_list():
    card = Card(yaml={"id": "x", "tags": []})
    card.tags.append("shed")
    assert card.yaml["tags"] == ["shed"]


def test_card_from_text_malformed_frontmatter_raises_valueerror():
    with pytest.raises(ValueError, match="malformed frontmatter"):
        Card.from_text("not a card")


def test_card_blob_defaults_none():
    assert Card(yaml={"id": "x"}).blob is None
    assert Card.from_text("---\nid: x\n---\nbody\n").blob is None


def test_card_copy_carries_blob():
    card = Card(yaml={"id": "x"}, body="b", blob=Path("/deck/x.pdf"))
    assert card.copy().blob == Path("/deck/x.pdf")


def test_card_blob_excluded_from_equality():
    a = Card(yaml={"id": "x"}, body="b", blob=Path("/deck-a/x.pdf"))
    b = Card(yaml={"id": "x"}, body="b", blob=Path("/deck-b/x.pdf"))
    assert a == b


def test_card_str_omits_blob():
    card = Card(yaml={"id": "x"}, body="hello\n", blob=Path("/deck/x.pdf"))
    assert "x.pdf" not in str(card)
    assert Card.from_text(str(card)).blob is None
