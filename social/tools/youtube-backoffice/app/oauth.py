"""YouTube OAuth2 helpers — refresh token → access token → API calls"""
import json, os, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET_PATH = os.path.join(HERE, "client_secret.json")
ENV_PATH = "/opt/data/youtube-backoffice.env"

def _load_creds():
    with open(SECRET_PATH) as f:
        return json.load(f)["installed"]

def _get_refresh_token() -> str:
    for line in open(ENV_PATH):
        if line.startswith("YOUTUBE_REFRESH_TOKEN="):
            return line.strip().split("=", 1)[1]
    raise RuntimeError("YOUTUBE_REFRESH_TOKEN not found in " + ENV_PATH)

def get_access_token() -> str:
    """Return a valid Bearer access token for YouTube Data API writes."""
    creds = _load_creds()
    rt = _get_refresh_token()
    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": rt,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(creds["token_uri"], data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    return resp["access_token"]

def reply_to_comment(comment_id: str, text: str) -> dict:
    """Post a public reply to a YouTube comment thread."""
    token = get_access_token()
    url = "https://www.googleapis.com/youtube/v3/comments?part=snippet"
    body = json.dumps({
        "snippet": {
            "parentId": comment_id,
            "textOriginal": text,
        }
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())
