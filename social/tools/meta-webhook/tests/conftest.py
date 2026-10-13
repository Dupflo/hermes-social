import pytest

from app.webhook_delivery_store import _reset_purge_state


@pytest.fixture(autouse=True)
def _isolated_processed_comments_database(monkeypatch, tmp_path):
    # s08: every webhook POST now records a delivery. Without this, the tests that do
    # not set PROCESSED_COMMENTS_DATABASE would write into data/ of the checkout.
    # Tests that set the variable themselves override this default.
    monkeypatch.setenv("PROCESSED_COMMENTS_DATABASE", str(tmp_path / "processed_comments.sqlite3"))
    _reset_purge_state()
    yield
    _reset_purge_state()
