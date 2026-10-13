"""s16 (console ADR 044) — pure normalisation of Graph conversations for the console inbox.

No I/O. Every malformed Graph entry is ignored or nulled, never an exception.
"""

import base64

import pytest

from app.inbox_read import (
    TEXT_MAX,
    as_int,
    bounded_str,
    decode_conversation_ref,
    decode_ref,
    encode_ref,
    normalize_conversation,
    normalize_message,
    normalize_peer,
)


PAGE_ID = "999"
IG_USER_ID = "17841400000000000"
OWNER_IDS = {PAGE_ID, IG_USER_ID, "555"}
OWNER_USERNAMES = {"dupflodev"}
SYNTHETIC_IG_ID = "aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6MTc4NDE0MDA=="


def raw_b64(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def peer(participants):
    return normalize_peer(participants, OWNER_IDS, OWNER_USERNAMES)


def message(raw):
    return normalize_message(raw, OWNER_IDS, OWNER_USERNAMES)


def conversation(raw):
    return normalize_conversation(raw, OWNER_IDS, OWNER_USERNAMES)


# --- opaque refs -----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["t_123", SYNTHETIC_IG_ID, "Q" * 600])
def test_encode_decode_round_trip(value):
    encoded = encode_ref(value)
    assert "=" not in encoded
    assert decode_ref(encoded) == value


@pytest.mark.parametrize("ref", ["", "!!", "_w", raw_b64(b"\xff\xfe"), "a" * 2049])
def test_invalid_refs_raise_value_error(ref):
    with pytest.raises(ValueError):
        decode_ref(ref)


@pytest.mark.parametrize("value", ["me/accounts", "x?y", "../x", "a..b", "a" * 201, "x y", "x#y"])
def test_forged_conversation_refs_are_refused(value):
    with pytest.raises(ValueError):
        decode_conversation_ref(raw_b64(value))


@pytest.mark.parametrize("value", ["t_123", "aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6MTc4NDE0MDA", "t_1.2:3-4"])
def test_valid_conversation_refs_decode_to_their_id(value):
    assert decode_conversation_ref(encode_ref(value)) == value


def test_conversation_ref_with_invalid_encoding_is_refused():
    with pytest.raises(ValueError):
        decode_conversation_ref("!!")


# --- coercions -------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [(10, 10), ("10", None), (10.0, None), (True, None), (None, None)])
def test_as_int(value, expected):
    assert as_int(value) == expected


def test_bounded_str():
    assert bounded_str("x" * 65, 64) == "x" * 64
    assert bounded_str("", 64) is None
    assert bounded_str(123, 64) is None
    assert bounded_str(None, 64) is None
    assert bounded_str("abc", 64) == "abc"


# --- peer ------------------------------------------------------------------------------


def test_peer_is_the_first_participant_that_is_not_the_page():
    result = peer(
        {
            "data": [
                {"id": PAGE_ID, "name": "Ma Page", "email": "page@facebook.com"},
                {"id": "fb-peer-1", "name": "Léa Dubois", "email": "lea@facebook.com"},
                {"id": "fb-peer-2", "name": "Autre"},
            ]
        }
    )
    assert result == {"id": "fb-peer-1", "name": "Léa Dubois", "username": None}
    assert "email" not in result


@pytest.mark.parametrize(
    "owner",
    [
        {"id": PAGE_ID, "name": "Page"},
        {"id": IG_USER_ID, "username": "someone"},
        {"id": "555", "name": "Owner from META_OWNER_IDS"},
        {"id": "777", "username": "DupFloDev"},
    ],
)
def test_owner_identities_are_never_the_peer(owner):
    result = peer({"data": [owner, {"id": "ig-peer-1", "username": "marc.automation"}]})
    assert result == {"id": "ig-peer-1", "name": None, "username": "marc.automation"}


@pytest.mark.parametrize("participants", [None, {}, {"data": []}, {"data": "x"}, [], "x"])
def test_no_participant_gives_null_fields(participants):
    assert peer(participants) == {"id": None, "name": None, "username": None}


def test_only_the_page_gives_null_fields():
    assert peer({"data": [{"id": PAGE_ID}]}) == {"id": None, "name": None, "username": None}


def test_peer_fields_are_bounded():
    result = peer({"data": [{"id": "x" * 201, "name": "n" * 300, "username": "u" * 300}]})
    assert result == {"id": None, "name": "n" * 200, "username": "u" * 200}


def test_peer_accepts_a_plain_list_and_skips_non_dicts():
    assert peer(["junk", {"id": "p1", "name": 42}]) == {"id": "p1", "name": None, "username": None}


# --- message ---------------------------------------------------------------------------


def base_message(**overrides):
    raw = {
        "id": "m_1",
        "message": "Bonjour",
        "from": {"id": "fb-peer-1", "name": "Léa"},
        "created_time": "2026-10-12T18:02:11+0000",
    }
    raw.update(overrides)
    return raw


def test_message_from_the_peer():
    assert message(base_message()) == {
        "id": "m_1",
        "text": "Bonjour",
        "from_me": False,
        "created_time": "2026-10-12T18:02:11+0000",
    }


def test_message_from_the_page_is_from_me():
    assert message(base_message(**{"from": {"id": PAGE_ID, "name": "Page"}}))["from_me"] is True
    assert message(base_message(**{"from": {"id": "1", "username": "DUPFLODEV"}}))["from_me"] is True


@pytest.mark.parametrize("text", ["", None, 42])
def test_empty_or_absent_text_is_null(text):
    assert message(base_message(message=text))["text"] is None


def test_absent_text_key_is_null():
    raw = base_message()
    del raw["message"]
    assert message(raw)["text"] is None


def test_text_is_truncated_to_5000():
    assert TEXT_MAX == 5000
    assert message(base_message(message="a" * 6000))["text"] == "a" * 5000


@pytest.mark.parametrize("raw_id", [None, "", "x" * 201, 123])
def test_message_without_a_valid_id_is_ignored(raw_id):
    assert message(base_message(id=raw_id)) is None


@pytest.mark.parametrize("created", [None, 1760292131, "", "x" * 65])
def test_message_without_a_valid_date_is_ignored(created):
    assert message(base_message(created_time=created)) is None


@pytest.mark.parametrize("raw", [None, "x", 1, []])
def test_non_dict_message_is_ignored(raw):
    assert message(raw) is None


def test_message_without_from_is_not_from_me():
    raw = base_message()
    del raw["from"]
    assert message(raw)["from_me"] is False


# --- conversation ----------------------------------------------------------------------


def base_conversation(**overrides):
    raw = {
        "id": "t_123",
        "updated_time": "2026-10-12T18:02:11+0000",
        "participants": {"data": [{"id": PAGE_ID, "name": "Page"}, {"id": "fb-peer-1", "name": "Léa"}]},
        "messages": {"data": [base_message(**{"from": {"id": PAGE_ID}}, message="Voici le lien")]},
    }
    raw.update(overrides)
    return raw


def test_conversation_is_normalised():
    assert conversation(base_conversation()) == {
        "ref": encode_ref("t_123"),
        "updated_time": "2026-10-12T18:02:11+0000",
        "peer": {"id": "fb-peer-1", "name": "Léa", "username": None},
        "last_message": {"text": "Voici le lien", "from_me": True, "created_time": "2026-10-12T18:02:11+0000"},
    }


def test_conversation_without_messages_has_no_last_message():
    raw = base_conversation()
    del raw["messages"]
    assert conversation(raw)["last_message"] is None
    assert conversation(base_conversation(messages={"data": []}))["last_message"] is None
    assert conversation(base_conversation(messages={"data": [{"message": "no id"}]}))["last_message"] is None


@pytest.mark.parametrize("raw_id", [None, "", "x" * 300, "me/accounts", "a..b", 42])
def test_conversation_without_a_valid_id_is_ignored(raw_id):
    assert conversation(base_conversation(id=raw_id)) is None


@pytest.mark.parametrize("updated", [None, 1, "x" * 65])
def test_conversation_without_a_valid_date_is_ignored(updated):
    assert conversation(base_conversation(updated_time=updated)) is None


@pytest.mark.parametrize("raw", [None, "x", []])
def test_non_dict_conversation_is_ignored(raw):
    assert conversation(raw) is None


def test_no_email_anywhere_in_the_output():
    raw = base_conversation(
        participants={"data": [{"id": "p1", "name": "Léa", "email": "lea@example.com"}]},
    )
    assert "email" not in repr(conversation(raw))
    assert "lea@example.com" not in repr(conversation(raw))
