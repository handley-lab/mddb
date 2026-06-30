import subprocess
import types

import pytest
import yaml

from mddb._merge import _merge_body, merge_cards
from mddb.card import Card


def c(yaml_dict, body=""):
    return Card(yaml=dict(yaml_dict), body=body)


def base_ours_theirs(base_yaml, ours_yaml, theirs_yaml, *, bb="", ob="", tb=""):
    return c(base_yaml, bb), c(ours_yaml, ob), c(theirs_yaml, tb)


ID = {"id": "11111111-1111-4111-8111-111111111111"}


def test_body_non_overlapping_edits_both_apply():
    base = c(ID, "line1\nline2\nline3\n")
    ours = c(ID, "OURS\nline2\nline3\n")
    theirs = c(ID, "line1\nline2\nTHEIRS\n")
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert clean
    assert "OURS" in text and "THEIRS" in text and "line2" in text


def test_body_same_line_conflict():
    base = c(ID, "shared\n")
    ours = c(ID, "ours-change\n")
    theirs = c(ID, "theirs-change\n")
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert not clean
    assert "<<<<<<<" in text and "=======" in text and ">>>>>>>" in text


def test_body_identical_is_clean_and_unchanged():
    base = c(ID, "same\n")
    text, clean = merge_cards(base, c(ID, "same\n"), c(ID, "same\n"), "card.md")
    assert clean
    assert Card.from_text(text).body == "same\n"


def test_body_no_trailing_newline_roundtrips():
    base = c(ID, "a\nb")
    ours = c(ID, "A\nb")
    theirs = c(ID, "a\nb")
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert clean
    assert Card.from_text(text).body == "A\nb"


def test_body_multi_hunk_conflict_is_conflict_not_error():
    base = c(ID, "a\nb\nc\nd\ne\nf\ng\n")
    ours = c(ID, "A\nb\nc\nd\ne\nf\nG\n")
    theirs = c(ID, "X\nb\nc\nd\ne\nf\nZ\n")
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert not clean
    assert text.count("<<<<<<<") == 2


def test_merge_body_error_raises(monkeypatch):
    def fake_run(args, **kwargs):
        return types.SimpleNamespace(
            returncode=255, stdout="", stderr="boom", args=args
        )

    monkeypatch.setattr("mddb._merge.subprocess.run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        _merge_body("a", "b", "c", "card.md")


def test_tags_deletion_wins():
    base, ours, theirs = base_ours_theirs(
        {**ID, "tags": ["a", "b"]}, {**ID, "tags": ["a"]}, {**ID, "tags": ["a", "b"]}
    )
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert clean
    assert Card.from_text(text).yaml["tags"] == ["a"]


def test_tags_base_absent_additions_union():
    base, ours, theirs = base_ours_theirs(
        {**ID, "tags": ["a"]}, {**ID, "tags": ["a", "b"]}, {**ID, "tags": ["a", "c"]}
    )
    text, _ = merge_cards(base, ours, theirs, "card.md")
    assert Card.from_text(text).yaml["tags"] == ["a", "b", "c"]


def test_tags_whole_key_delete_removes_base_tags():
    base, ours, theirs = base_ours_theirs(
        {**ID, "tags": ["a", "b"]}, {**ID}, {**ID, "tags": ["a", "b"]}
    )
    text, _ = merge_cards(base, ours, theirs, "card.md")
    assert "tags" not in Card.from_text(text).yaml


def test_tags_duplicates_collapse():
    base, ours, theirs = base_ours_theirs({**ID}, {**ID, "tags": ["b", "b"]}, {**ID})
    text, _ = merge_cards(base, ours, theirs, "card.md")
    assert Card.from_text(text).yaml["tags"] == ["b"]


def test_tags_non_list_raises():
    base, ours, theirs = base_ours_theirs({**ID}, {**ID, "tags": "foo"}, {**ID})
    with pytest.raises(ValueError):
        merge_cards(base, ours, theirs, "card.md")


def test_tags_non_string_item_raises():
    base, ours, theirs = base_ours_theirs({**ID}, {**ID, "tags": [1]}, {**ID})
    with pytest.raises(ValueError):
        merge_cards(base, ours, theirs, "card.md")


def test_empty_tags_all_sides_omitted():
    base, ours, theirs = base_ours_theirs(
        {**ID, "tags": []}, {**ID, "tags": []}, {**ID, "tags": []}
    )
    text, _ = merge_cards(base, ours, theirs, "card.md")
    assert "tags" not in Card.from_text(text).yaml


def test_empty_tags_base_absent_ours_empty_omitted():
    base, ours, theirs = base_ours_theirs({**ID}, {**ID, "tags": []}, {**ID})
    text, _ = merge_cards(base, ours, theirs, "card.md")
    assert "tags" not in Card.from_text(text).yaml


def test_empty_tags_cleared_one_side_unchanged_other_omitted():
    base, ours, theirs = base_ours_theirs(
        {**ID, "tags": ["x"]}, {**ID, "tags": []}, {**ID, "tags": ["x"]}
    )
    text, _ = merge_cards(base, ours, theirs, "card.md")
    assert "tags" not in Card.from_text(text).yaml


def test_empty_tags_raw_other_side_addition_kept():
    base, ours, theirs = base_ours_theirs(
        {**ID}, {**ID, "tags": ["x"]}, {**ID, "tags": []}
    )
    text, _ = merge_cards(base, ours, theirs, "card.md")
    assert Card.from_text(text).yaml["tags"] == ["x"]


def test_scalar_only_ours_changed():
    base, ours, theirs = base_ours_theirs(
        {**ID, "summary": "a"}, {**ID, "summary": "b"}, {**ID, "summary": "a"}
    )
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert clean
    assert Card.from_text(text).yaml["summary"] == "b"


def test_scalar_only_theirs_changed():
    base, ours, theirs = base_ours_theirs(
        {**ID, "summary": "a"}, {**ID, "summary": "a"}, {**ID, "summary": "c"}
    )
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert clean
    assert Card.from_text(text).yaml["summary"] == "c"


def test_scalar_both_same_change():
    base, ours, theirs = base_ours_theirs(
        {**ID, "summary": "a"}, {**ID, "summary": "b"}, {**ID, "summary": "b"}
    )
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert clean
    assert Card.from_text(text).yaml["summary"] == "b"


def test_scalar_added_one_side():
    base, ours, theirs = base_ours_theirs({**ID}, {**ID, "status": "open"}, {**ID})
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert clean
    assert Card.from_text(text).yaml["status"] == "open"


def test_scalar_deleted_one_side_unchanged_other():
    base, ours, theirs = base_ours_theirs(
        {**ID, "status": "open"}, {**ID}, {**ID, "status": "open"}
    )
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert clean
    assert "status" not in Card.from_text(text).yaml


def test_scalar_both_differ_conflicts():
    base, ours, theirs = base_ours_theirs(
        {**ID, "summary": "a"}, {**ID, "summary": "b"}, {**ID, "summary": "c"}
    )
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert not clean
    assert "<<<<<<<" in text


def test_id_existing_equal_passes_and_emitted():
    base, ours, theirs = base_ours_theirs(
        {**ID, "summary": "x"}, {**ID, "summary": "x"}, {**ID, "summary": "x"}
    )
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert clean
    assert Card.from_text(text).yaml["id"] == ID["id"]


def test_id_ours_changed_raises():
    base = c({**ID})
    ours = c({"id": "22222222-2222-4222-8222-222222222222"})
    theirs = c({**ID})
    with pytest.raises(ValueError):
        merge_cards(base, ours, theirs, "card.md")


def test_id_theirs_changed_raises():
    base = c({**ID})
    ours = c({**ID})
    theirs = c({"id": "22222222-2222-4222-8222-222222222222"})
    with pytest.raises(ValueError):
        merge_cards(base, ours, theirs, "card.md")


def test_id_both_changed_to_same_non_base_raises():
    new = {"id": "22222222-2222-4222-8222-222222222222"}
    with pytest.raises(ValueError):
        merge_cards(c({**ID}), c({**new}), c({**new}), "card.md")


def test_id_add_add_equal_carries():
    base = c({})
    ours = c({**ID, "summary": "s"})
    theirs = c({**ID, "summary": "s"})
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert clean
    assert Card.from_text(text).yaml["id"] == ID["id"]


def test_id_add_add_differ_conflicts():
    base = c({})
    ours = c({"id": "11111111-1111-4111-8111-111111111111"})
    theirs = c({"id": "22222222-2222-4222-8222-222222222222"})
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert not clean
    assert "<<<<<<<" in text


def test_key_order_preserves_ours():
    base, ours, theirs = base_ours_theirs(
        {**ID, "title": "T", "summary": "S"},
        {**ID, "title": "T", "summary": "S", "z": 1, "a": 2},
        {**ID, "title": "T", "summary": "S"},
    )
    text, _ = merge_cards(base, ours, theirs, "card.md")
    keys = list(Card.from_text(text).yaml)
    assert keys == ["id", "title", "summary", "z", "a"]


def test_clean_output_reparses():
    base, ours, theirs = base_ours_theirs(
        {**ID, "title": "T", "summary": "S", "tags": ["x"]},
        {**ID, "title": "T", "summary": "S2", "tags": ["x", "y"]},
        {**ID, "title": "T", "summary": "S", "tags": ["x"]},
    )
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert clean
    parsed = Card.from_text(text)
    assert parsed.yaml["summary"] == "S2"
    assert parsed.yaml["tags"] == ["x", "y"]


def test_frontmatter_conflict_output_does_not_parse():
    base, ours, theirs = base_ours_theirs(
        {**ID, "summary": "a"}, {**ID, "summary": "b"}, {**ID, "summary": "c"}
    )
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert not clean
    with pytest.raises(yaml.YAMLError):
        Card.from_text(text)


def test_body_only_conflict_still_parses_with_markers_in_body():
    base = c(ID, "shared\n")
    ours = c(ID, "ours\n")
    theirs = c(ID, "theirs\n")
    text, clean = merge_cards(base, ours, theirs, "card.md")
    assert not clean
    parsed = Card.from_text(text)
    assert parsed.yaml["id"] == ID["id"]
    assert "<<<<<<<" in parsed.body and ">>>>>>>" in parsed.body
