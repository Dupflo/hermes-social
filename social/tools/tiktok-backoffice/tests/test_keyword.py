from app.keyword import contains_keyword, normalize_text


def test_normalize_text_casefolds_and_collapses_spaces():
    assert normalize_text("  Proxy   PLEASE ") == "proxy please"


def test_contains_keyword_matches_whole_word_case_insensitive():
    assert contains_keyword("Proxy", "proxy") is True
    assert contains_keyword("tu peux envoyer proxy ?", "proxy") is True
    assert contains_keyword("proxymania", "proxy") is False
