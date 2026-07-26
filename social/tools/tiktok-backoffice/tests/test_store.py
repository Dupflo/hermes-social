from app.models import ReplyDraft, ReviewStatus, TikTokComment
from app.store import TikTokBackofficeStore


def test_add_comment_and_next_pending(tmp_path):
    store = TikTokBackofficeStore(tmp_path / "tiktok.sqlite3")
    changed = store.add_comment(TikTokComment(video_url="https://www.tiktok.com/@dupflodev/video/123", video_id="123", comment_id="c1", author="@alice", text="proxy"))

    assert changed is True
    assert store.next_pending()["comment_id"] == "c1"


def test_save_draft_marks_comment_drafted(tmp_path):
    store = TikTokBackofficeStore(tmp_path / "tiktok.sqlite3")
    store.add_comment(TikTokComment(video_url="https://example.test/video", comment_id="c1", author=None, text="proxy"))

    store.save_draft(ReplyDraft(comment_id="c1", keyword="proxy", reply_text="Réponse brouillon"))

    assert store.get_comment("c1")["status"] == ReviewStatus.DRAFTED
    assert store.get_draft("c1")["reply_text"] == "Réponse brouillon"
