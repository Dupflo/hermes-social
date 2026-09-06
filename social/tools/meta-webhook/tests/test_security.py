from app.security import verify_meta_signature


def test_verify_meta_signature_fails_closed_when_app_secret_is_missing():
    assert verify_meta_signature(b"{}", None, "") is False


def test_verify_meta_signature_accepts_sha256_header():
    assert verify_meta_signature(
        b'{"ok":true}',
        "sha256=f6b4a2841c93f8bf2fb8f2c13d8fb0b6c8e8019f09ee405d248daa8385fad638",
        "secret",
    ) is True


def test_verify_meta_signature_accepts_sha1_legacy_header():
    assert verify_meta_signature(
        b'{"ok":true}',
        "sha1=168fa90587936f7a90a10bed22b97033920f7384",
        "secret",
    ) is True


def test_verify_meta_signature_rejects_bad_header():
    assert verify_meta_signature(b'{"ok":true}', "md5=bad", "secret") is False
