"""TikTok pages of the console job `tiktok-reply`: selectors and read-only page scripts (s24, console ADR 055, P9).

This module is the ONLY place that knows the video comments panel, the profile page and the DM thread, so a
selector can be fixed without touching the reply logic (app/tiktok_reply.py) or the gestures
(app/tiktok_reply_page.py). The heuristics are taken from the versioned legacy processor
scripts/process_tiktok_review_one.py (anchors cited per script), rewritten as read-only snapshots:

- comments panel opener: `[data-e2e="comment-icon"]`, then an aria-label "comment" (l. 81-94);
- target comment: author from the `/@handle` link, text from the content wrapper, containers
  `[class*="DivCommentItemWrapper"], [data-e2e*="comment"]` (l. 105-113); "View N replies" (l. 116-117);
  owner replies near the target (l. 118-120), the owner handle being a parameter, never written here;
- profile "Message" button (l. 136-141), DM editor (`textarea, [contenteditable], [role=textbox]`, l. 146, 158),
  send button `dm-new-send-btn` (l. 166-173), send error wording (l. 177);
- inline "Reply" button and "Post" button of a comment (l. 196-209).

Every script only READS the page and returns JSON: no click, no event, no value written, no « J’aime », no
editing command. Each element the job must act on is returned with a unique CSS path computed from the DOM
(`cssPath`), handed to `CamofoxClient.click_selector` / `type_text`, and with its rectangle centre (fallback of the
s22 `console-mouse` plugin). None of these heuristics could be checked against the real TikTok UI (story
BLOCKERS): every action is verified in the page afterwards, and a mismatch ends `not_verified`.
"""

from __future__ import annotations

import json

VIDEO_SETTLE_S = 6.0
PANEL_SETTLE_S = 2.5
REPLIES_SETTLE_S = 1.0
PROFILE_SETTLE_S = 6.0
THREAD_SETTLE_S = 4.0
AFTER_SEND_SETTLE_S = 4.0
MAX_VIEW_REPLIES_CLICKS = 2

PROFILE_URL = "https://www.tiktok.com/{handle}"

_HELPERS = r"""
 function clean(s){return String(s||'').replace(/\s+/g,' ').trim()}
 function vis(el){const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&r.x>=0&&r.y>=0}
 function cssPath(el){
  if(!el||!el.tagName)return null;
  const parts=[];
  for(let n=el;n&&n.nodeType===1&&n!==document.documentElement;n=n.parentElement){
   if(n.id&&/^[A-Za-z][A-Za-z0-9_-]*$/.test(n.id)&&document.querySelectorAll('#'+n.id).length===1){parts.unshift('#'+n.id);break}
   let i=1; for(let s=n.previousElementSibling;s;s=s.previousElementSibling){if(s.tagName===n.tagName)i++}
   parts.unshift(n.tagName.toLowerCase()+':nth-of-type('+i+')');
  }
  if(parts.length&&!parts[0].startsWith('#'))parts.unshift('html');
  return parts.join(' > ');
 }
 function handle(el){if(!el)return null; const r=el.getBoundingClientRect(); return {selector:cssPath(el),x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}}
 function loginGate(){return /\bLog\s*in\b|Sign\s*up|Log\s*in\s+to\s+comment/i.test(document.body.innerText||'')}
 function authorFrom(el){const links=Array.from(el.querySelectorAll('a[href^="/@"], a[href*="tiktok.com/@"]')); for(const a of links){const h=a.getAttribute('href')||''; const m=h.match(/\/@([^/?#]+)/); if(m)return '@'+decodeURIComponent(m[1]); const t=clean(a.innerText||a.textContent); if(t&&!t.includes(' '))return t.startsWith('@')?t:'@'+t} return null}
 function commentTextFrom(container){const content=container.querySelector('[class*="DivCommentContentWrapper"]')||container; const user=clean((content.querySelector('[data-e2e*="comment-username"], [class*="DivUsernameContentWrapper"]')||{}).innerText||''); let txt=clean(content.innerText||content.textContent); if(user&&txt.startsWith(user))txt=clean(txt.slice(user.length)); return clean(txt.replace(/\s+(?:\d+[smhdw]|\d+-\d+|Yesterday|Today)\s+ago\s+Reply(?:\s+\d+)?(?:\s+View\s+\d+\s+repl(?:y|ies))?\s*$/i,'').replace(/\s+Reply(?:\s+\d+)?(?:\s+View\s+\d+\s+repl(?:y|ies))?\s*$/i,'').replace(/\s+View\s+\d+\s+repl(?:y|ies)\s*$/i,'').replace(/\s+\d{1,2}-\d{1,2}\s*$/i,''));}
 function editors(root){return Array.from((root||document).querySelectorAll('textarea,[contenteditable="true"],div[contenteditable],[role="textbox"]')).filter(vis)}
"""

# Comments panel of a video: one entry per top-level comment, with the replies shown under it (P9). Besides the
# structural replies, an element by the owner within 520 px under a comment counts as one of its replies (l. 118-119).
_COMMENTS_BODY = r"""
 const all=Array.from(document.querySelectorAll('[class*="DivCommentItemWrapper"], [data-e2e*="comment-level-1"]')).filter(vis);
 const top=all.filter(el=>!el.closest('[class*="DivReplyContainer"]')&&!all.some(o=>o!==el&&o.contains(el)));
 const icon=document.querySelector('[data-e2e="comment-icon"]')||Array.from(document.querySelectorAll('[aria-label]')).filter(vis).find(el=>/Read or add comments|comments?/i.test(el.getAttribute('aria-label')||''));
 const comments=top.map(el=>{
  const wrap=el.closest('[class*="DivCommentObjectWrapper"]')||el.parentElement||el;
  const replies=Array.from(wrap.querySelectorAll('[class*="DivReplyContainer"] [class*="DivCommentItemWrapper"], [data-e2e*="comment-level-2"]')).filter(vis).map(r=>({author:authorFrom(r),text:commentTextFrom(r)}));
  const tr=el.getBoundingClientRect(); const next=top[top.indexOf(el)+1]; const limit=next?next.getBoundingClientRect().y:tr.y+520;
  const near=Array.from(document.querySelectorAll('[class*="DivCommentItemWrapper"], [data-e2e*="comment"]')).filter(vis).filter(n=>n!==el&&!top.includes(n)&&(authorFrom(n)||'').toLowerCase()===OWNER.toLowerCase()).filter(n=>{const r=n.getBoundingClientRect(); return r.y>tr.y&&r.y<limit});
  for(const n of near){const t=commentTextFrom(n); if(!replies.some(r=>r.text===t&&(r.author||'').toLowerCase()===OWNER.toLowerCase()))replies.push({author:authorFrom(n),text:t})}
  const ctrls=Array.from(el.querySelectorAll('button,[role=button],span,div')).filter(vis);
  const reply=ctrls.find(c=>clean(c.innerText||c.textContent)==='Reply'||/comment-reply/i.test(c.getAttribute('data-e2e')||''));
  const views=Array.from(wrap.querySelectorAll('button,[role=button],span,div,p')).filter(vis).filter(c=>/^View\s+\d+\s+repl(?:y|ies)$/i.test(clean(c.innerText||c.textContent))).slice(0,2);
  return {author:authorFrom(el),text:commentTextFrom(el),reply_button:handle(reply),view_replies:views.map(handle),replies};
 });
 return {login_gate:loginGate(),panel_open:all.length>0||/Add comment/i.test(document.body.innerText||''),comment_icon:handle(icon),comments};
"""


def comments_js(owner: str) -> str:
    """Snapshot of the comments panel; the owner handle is injected by json.dumps (P9), never written here."""
    return "(() => {" + _HELPERS + f" const OWNER={json.dumps(owner)};" + _COMMENTS_BODY + "})()"


# Inline reply editor opened under a comment, and its "Post" button (l. 201-209).
REPLY_EDITOR_JS = (
    "(() => {"
    + _HELPERS
    + r"""
 const eds=editors().filter(e=>/reply/i.test((e.getAttribute('placeholder')||'')+(e.getAttribute('aria-placeholder')||'')+(e.getAttribute('aria-label')||''))||e.closest('[class*="DivReplyContainer"], [class*="DivCommentInputWrapper"], [class*="CommentInput"]'));
 const ed=eds[eds.length-1]||null;
 let post=null;
 if(ed){const er=ed.getBoundingClientRect(); post=Array.from(document.querySelectorAll('button,[role=button],div[data-e2e*="post"],span')).filter(vis).filter(c=>/^Post$/i.test(clean(c.innerText||c.textContent))||/comment-post/i.test(c.getAttribute('data-e2e')||'')).filter(c=>{const r=c.getBoundingClientRect(); return r.y>er.y-80&&r.y<er.y+160}).sort((a,b)=>Math.abs(a.getBoundingClientRect().y-er.y)-Math.abs(b.getBoundingClientRect().y-er.y))[0]||null}
 return {login_gate:loginGate(),editor:ed?Object.assign(handle(ed),{text:clean(ed.innerText||ed.textContent||ed.value||'')}):null,post_button:handle(post)};
"""
    + "})()"
)

# Profile page: the "Message" button (l. 136-141).
PROFILE_JS = (
    "(() => {"
    + _HELPERS
    + r"""
 const ctrls=Array.from(document.querySelectorAll('[data-e2e="message-button"],button,[role=button],a')).filter(vis);
 const msg=ctrls.find(c=>(c.getAttribute('data-e2e')||'')==='message-button')||ctrls.find(c=>/^Message$/i.test(clean(c.innerText||c.textContent))||/\bMessage\b/i.test(c.getAttribute('aria-label')||'')||/messages\?/i.test(c.getAttribute('href')||''));
 return {login_gate:loginGate(),message_button:handle(msg)};
"""
    + "})()"
)

# DM thread: editor text, last outgoing bubbles (oldest first), send button, send error (l. 146-179).
THREAD_JS = (
    "(() => {"
    + _HELPERS
    + r"""
 const ed=editors().filter(e=>!e.closest('[class*="DivCommentInputWrapper"]'))[0]||null;
 const items=Array.from(document.querySelectorAll('[data-e2e="chat-item"], [class*="DivChatItemWrapper"]')).filter(vis);
 const box=(items[0]&&items[0].parentElement)?items[0].parentElement.getBoundingClientRect():{x:0,width:window.innerWidth};
 const mid=box.x+box.width/2;
 const outgoing=items.filter(it=>{const r=it.getBoundingClientRect(); return /my-?message|self/i.test(String(it.className||''))||r.x+r.width/2>mid}).map(it=>clean(it.innerText||it.textContent)).slice(-20);
 const send=Array.from(document.querySelectorAll('[data-e2e*="dm-new-send-btn"],[class*="dm-new-send-btn"],[aria-label*="Send" i]')).filter(vis)[0]||null;
 const body=clean(document.body.innerText||'');
 return {login_gate:loginGate(),editor:ed?Object.assign(handle(ed),{text:clean(ed.innerText||ed.textContent||ed.value||'')}):null,outgoing,send_button:handle(send),error:/failed to send|couldn.t send|not sent|can.t send|cannot send|message could not be sent/i.test(body)};
"""
    + "})()"
)


def profile_url(handle: str) -> str:
    return PROFILE_URL.format(handle=handle)
