from app.processor import public_reply_for_platform, select_public_reply_variant


def test_public_reply_variants_are_deterministic_by_comment_id():
    base = "A---VARIANT---B---VARIANT---C"
    assert select_public_reply_variant(base, "comment-1") == select_public_reply_variant(base, "comment-1")
    assert select_public_reply_variant(base, "comment-1") in {"A", "B", "C"}


def test_public_reply_variant_keeps_instagram_dm_confirmation_safe():
    base = "C'est envoyé ! Check tes messages privés — regarde aussi dans les invitations / demandes de message Messenger.---VARIANT---Super, je t’envoie ça en privé."
    assert "Messenger" not in public_reply_for_platform("instagram", base, "comment-2")


def test_facebook_default_confirmation_mentions_messenger_invitations():
    assert "Messenger" in public_reply_for_platform("facebook", "C'est envoyé ! Check tes messages privés !", "comment-1")
