import json

from app.cli import run
from app.discovery import parse_profile_video_links


def test_parse_profile_video_links_dedupes_and_normalizes():
    html = """
    <a href=\"/@dupflodev/video/123\">one</a>
    <a href=\"https://www.tiktok.com/@dupflodev/video/456?lang=fr\">two</a>
    <script>{\"url\":\"https:\\\\/\\\\/www.tiktok.com\\\\/@dupflodev\\\\/video\\\\/789\"}</script>
    <a href=\"/@other/video/999\">ignore other user</a>
    <a href=\"/@dupflodev/video/123\">duplicate</a>
    """

    videos = parse_profile_video_links(html, profile="@dupflodev")

    assert [video.video_id for video in videos] == ["123", "456", "789"]
    assert [video.video_url for video in videos] == [
        "https://www.tiktok.com/@dupflodev/video/123",
        "https://www.tiktok.com/@dupflodev/video/456",
        "https://www.tiktok.com/@dupflodev/video/789",
    ]


def test_discover_videos_from_html_file_adds_to_registry(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    html_file = tmp_path / "profile.html"
    html_file.write_text('<a href="/@dupflodev/video/123">one</a><a href="/@dupflodev/video/456">two</a>')

    result = json.loads(run(["--db", str(db), "discover-videos", "--profile", "@dupflodev", "--html-file", str(html_file)]))

    assert result == {"ok": True, "profile": "@dupflodev", "found": 2, "added": 2}
    videos = json.loads(run(["--db", str(db), "list-videos"]))["items"]
    assert {video["video_id"] for video in videos} == {"123", "456"}


def test_discover_videos_is_idempotent(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    html_file = tmp_path / "profile.html"
    html_file.write_text('<a href="/@dupflodev/video/123">one</a>')

    first = json.loads(run(["--db", str(db), "discover-videos", "--profile", "@dupflodev", "--html-file", str(html_file)]))
    second = json.loads(run(["--db", str(db), "discover-videos", "--profile", "@dupflodev", "--html-file", str(html_file)]))

    assert first["added"] == 1
    assert second["added"] == 0


def test_parse_profile_video_links_extracts_img_alt_caption():
    html = '<a href="/@dupflodev/video/123"><img alt="Commente proxy et je t envoie le repo"></a>'

    videos = parse_profile_video_links(html, profile="dupflodev")

    assert videos[0].caption == "Commente proxy et je t envoie le repo"
