import hashlib
import hmac


def verify_meta_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    if not app_secret or not signature_header:
        return False

    if signature_header.startswith("sha256="):
        expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        received = signature_header.removeprefix("sha256=")
        return hmac.compare_digest(expected, received)

    # Some Meta webhook products/proxies still use the legacy SHA-1 header.
    if signature_header.startswith("sha1="):
        expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha1).hexdigest()
        received = signature_header.removeprefix("sha1=")
        return hmac.compare_digest(expected, received)

    return False


def verify_meta_signature_any(raw_body: bytes, signature_headers: list[str | None], app_secrets: list[str]) -> bool:
    """Accept the payload if any signature header matches any configured secret.

    Meta signs Facebook Login webhooks with the Meta app secret, but webhooks of the
    "Instagram API with Instagram Login" product with the separate Instagram app secret.
    """
    return any(
        verify_meta_signature(raw_body, header, secret)
        for header in signature_headers
        if header
        for secret in app_secrets
        if secret
    )
