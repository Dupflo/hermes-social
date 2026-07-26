from app.comment_review_classifier import classify_comment_for_review


def test_classifier_keeps_direct_resource_requests():
    decision = classify_comment_for_review(
        text="Le nom du site svp",
        has_owner_reply=False,
        matches_active_campaign=False,
        already_terminal=False,
    )

    assert decision.should_store is True
    assert decision.reason == "direct_request"
    assert decision.score == 80


def test_classifier_prioritizes_automation_complaints_even_with_owner_reply():
    decision = classify_comment_for_review(
        text="C'est nul tes tutos lorsqu'on commente tu nous envoies que dalle",
        has_owner_reply=True,
        matches_active_campaign=True,
        already_terminal=False,
    )

    assert decision.should_store is True
    assert decision.reason == "automation_complaint"
    assert decision.score == 100


def test_classifier_keeps_business_intent():
    decision = classify_comment_for_review(
        text="Je t'envoie un message privé, j'aimerais travailler avec toi",
        has_owner_reply=False,
        matches_active_campaign=False,
        already_terminal=False,
    )

    assert decision.should_store is True
    assert decision.reason == "business_intent"
    assert decision.score == 90


def test_classifier_keeps_short_unknown_keyword_but_ignores_noise():
    keyword = classify_comment_for_review(
        text="Repo",
        has_owner_reply=False,
        matches_active_campaign=False,
        already_terminal=False,
    )
    noise = classify_comment_for_review(
        text="Merci ! 🙂",
        has_owner_reply=False,
        matches_active_campaign=False,
        already_terminal=False,
    )

    assert keyword.should_store is True
    assert keyword.reason == "possible_unconfigured_keyword"
    assert keyword.score == 40
    assert noise.should_store is False
    assert noise.reason == "noise"


def test_classifier_ignores_already_automated_or_answered_comments_unless_complaint():
    active_campaign = classify_comment_for_review(
        text="Proxy",
        has_owner_reply=False,
        matches_active_campaign=True,
        already_terminal=False,
    )
    owner_replied = classify_comment_for_review(
        text="Le nom du site svp",
        has_owner_reply=True,
        matches_active_campaign=False,
        already_terminal=False,
    )
    terminal = classify_comment_for_review(
        text="Le nom du site svp",
        has_owner_reply=False,
        matches_active_campaign=False,
        already_terminal=True,
    )

    assert active_campaign.should_store is False
    assert active_campaign.reason == "automated_campaign"
    assert owner_replied.should_store is False
    assert owner_replied.reason == "already_replied"
    assert terminal.should_store is False
    assert terminal.reason == "already_terminal"

from app.comment_review_classifier import classify_comment_for_review


def test_skeptical_or_sarcastic_contest_comment_is_reviewed():
    decision = classify_comment_for_review(
        text="Belle façon de faire marcher son réseau pour gagner 30e de crédit haha",
        has_owner_reply=False,
        matches_active_campaign=False,
        already_terminal=False,
    )

    assert decision.should_store is True
    assert decision.reason == "skeptical_or_sarcastic"
    assert decision.score == 70
