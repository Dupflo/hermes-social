#!/usr/bin/env python3
"""YouTube cron: scan comments, reply with resource link"""
import json, os, sys, urllib.request, urllib.parse, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from oauth import reply_to_comment

ENV_PATH = "/opt/data/youtube-backoffice.env"
DB_PATH = "/opt/data/youtube-backoffice/processed_comments.sqlite3"

def env_val(key):
    for line in open(ENV_PATH):
        if line.startswith(key + "="):
            return line.strip().split("=", 1)[1]
    return ""

API_KEY = env_val("YOUTUBE_API_KEY")
CHANNEL_ID = env_val("YOUTUBE_CHANNEL_ID")
PUBLIC_REPLY = env_val("PUBLIC_REPLY_TEXT") or "Voici le lien :"
INTEREST_ONLY = {k.strip() for k in env_val("INTEREST_ONLY_KEYWORDS").split(",") if k.strip()}
CAMPAIGN_URLS = {
    "proxy": "https://dupflodev.vercel.app/1jour1skill?ep=ep9",
    "markitdown": "https://dupflodev.vercel.app/1jour1skill?ep=ep6",
    "system": "https://dupflodev.vercel.app/1jour1skill?ep=ep8",
    "gstack": "https://dupflodev.vercel.app/1jour1skill?ep=ep7",
    "voicebox": "https://dupflodev.vercel.app/1jour1skill?ep=ep10",
    "graphify": "https://dupflodev.vercel.app/1jour1skill?ep=ep11",
    "obsidian": "https://dupflodev.vercel.app/1jour1skill?ep=ep12",
    "competence": "https://support.claude.com/fr/articles/12512180-utiliser-les-competences-dans-claude",
}
VIDEO_CAMPAIGN_URLS = {
    # Same keyword as Claude competence video, different resource.
    "5Rvl_05NPSU": "https://help.openai.com/en/articles/20001066-skills-in-chatgpt",
}
# Backward compatibility: RESOURCE_URL overrides Proxy only if explicitly set.
if env_val("RESOURCE_URL"):
    CAMPAIGN_URLS["proxy"] = env_val("RESOURCE_URL")
KEYWORDS = {
    "proxy":"proxy","proxi":"proxy",
    "markitdown":"markitdown","markindown":"markitdown","markdown":"markitdown",
    "system":"system","système":"system","systeme":"system",
    "gstack":"gstack","gstak":"gstack",
    "voicebox":"voicebox",
    "graphify":"graphify","graphity":"graphify","graphiphy":"graphify","graphily":"graphify",
    "obsidian":"obsidian",
    "competence":"competence","compétence":"competence","competences":"competence","compétences":"competence",
}

def fetch(path, params=None):
    p = {"key": API_KEY, **(params or {})}
    url = f"https://www.googleapis.com/youtube/v3/{path}?{urllib.parse.urlencode(p)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS processed_comments (
            platform TEXT NOT NULL DEFAULT 'youtube',
            comment_id TEXT NOT NULL,
            keyword TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            like_sent INTEGER NOT NULL DEFAULT 0,
            public_reply_sent INTEGER NOT NULL DEFAULT 0,
            dm_sent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            processed_at TEXT,
            error_message TEXT,
            PRIMARY KEY (platform, comment_id)
        )
    """)
    con.commit()
    return con

def run_scan():
    con = init_db()
    replied = 0
    uploads = fetch("search", {"channelId": CHANNEL_ID, "part": "snippet", "order": "date", "maxResults": 10, "type": "video"})
    for item in uploads.get("items", []):
        vid = item["id"]["videoId"]
        try:
            comments = fetch("commentThreads", {"videoId": vid, "part": "snippet", "maxResults": 40, "order": "time"})
        except:
            continue
        for cmt in comments.get("items", []):
            s2 = cmt["snippet"]["topLevelComment"]["snippet"]
            cid = cmt["id"]
            author = s2.get("authorDisplayName", "?")
            text = s2.get("textDisplay", "")
            tl = text.lower().replace("é","e").replace("è","e")
            cur = con.execute("SELECT status FROM processed_comments WHERE platform='youtube' AND comment_id=?", (cid,))
            if cur.fetchone():
                continue
            matched = None
            for kw, campaign in KEYWORDS.items():
                if kw in tl:
                    matched = campaign
                    break
            if not matched:
                continue
            if matched in INTEREST_ONLY:
                con.execute("INSERT OR IGNORE INTO processed_comments (platform, comment_id, keyword, status) VALUES ('youtube', ?, ?, 'ignored')", (cid, matched))
                con.commit()
                continue
            reply_text = f"{PUBLIC_REPLY} {VIDEO_CAMPAIGN_URLS.get(vid, CAMPAIGN_URLS.get(matched, CAMPAIGN_URLS['proxy']))}"
            try:
                resp = reply_to_comment(cid, reply_text)
                reply_id = resp.get("id", "?")
                con.execute("INSERT OR REPLACE INTO processed_comments (platform, comment_id, keyword, status, public_reply_sent, dm_sent, processed_at) VALUES ('youtube', ?, ?, 'processed', 1, 0, CURRENT_TIMESTAMP)", (cid, matched))
                con.commit()
                print(f"✅ @{author} ({cid}) → replied id={reply_id}")
                replied += 1
            except Exception as e:
                error = str(e)[:200]
                con.execute("INSERT OR IGNORE INTO processed_comments (platform, comment_id, keyword, status, error_message) VALUES ('youtube', ?, ?, 'failed', ?)", (cid, matched, error))
                con.commit()
                print(f"❌ @{author} ({cid}) → {error}")
    con.close()
    return replied

if __name__ == "__main__":
    r = run_scan()
    if r > 0:
        print(f"\nDone: {r} replies sent")
