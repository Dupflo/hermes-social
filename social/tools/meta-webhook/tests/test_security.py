from app.security import verify_meta_signature


def test_verify_meta_signature_fails_closed_when_app_secret_is_missing():
    assert verify_meta_signature(b"{}", None, "") is False
