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

Login gate (s25, s24 m2, console ADR 059, P6): a visible `a`, `button` or `[role="button"]` whose own text is exactly
« Log in », « Sign up » or « Log in to comment » (case-insensitive), outside any user content (comments, replies, DM
bubbles, editors) — never a regex on the page text, which a comment or a message could hold.

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
AFTER_SEND_SETTLE_S = 4.0
# s30 (console ADR 064, D3, A3): after the « Message » click, the editor of the conversation is read again every
# DM_EDITOR_POLL_S up to DM_EDITOR_WAIT_S (captcha time excluded), DM_OPEN_ATTEMPTS clicks at most; after the send
# click, the thread is read again every DM_EDITOR_POLL_S up to DM_VERIFY_WAIT_S (no gesture).
DM_EDITOR_POLL_S = 1.0
DM_EDITOR_WAIT_S = 15.0
DM_OPEN_ATTEMPTS = 2
DM_VERIFY_WAIT_S = 10.0
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
 function loginGate(){const words=['log in','sign up','log in to comment']; const user='[class*="DivCommentItemWrapper"],[class*="DivReplyContainer"],[data-e2e*="comment-level"],[data-e2e="chat-item"],[class*="DivChatItemWrapper"],textarea,[contenteditable],[role="textbox"]'; return Array.from(document.querySelectorAll('a,button,[role="button"]')).some(el=>vis(el)&&words.includes(clean(el.innerText||el.textContent).toLowerCase())&&!el.closest(user))}
 function authorFrom(el){const links=Array.from(el.querySelectorAll('a[href^="/@"], a[href*="tiktok.com/@"]')); for(const a of links){const h=a.getAttribute('href')||''; const m=h.match(/\/@([^/?#]+)/); if(m)return '@'+decodeURIComponent(m[1]); const t=clean(a.innerText||a.textContent); if(t&&!t.includes(' '))return t.startsWith('@')?t:'@'+t} return null}
 function commentTextFrom(container){const content=container.querySelector('[class*="DivCommentContentWrapper"]')||container; const user=clean((content.querySelector('[data-e2e*="comment-username"], [class*="DivUsernameContentWrapper"]')||{}).innerText||''); let txt=clean(content.innerText||content.textContent); if(user&&txt.startsWith(user))txt=clean(txt.slice(user.length)); return clean(txt.replace(/\s+(?:\d+[smhdw]|\d+-\d+|Yesterday|Today)\s+ago\s+Reply(?:\s+\d+)?(?:\s+View\s+\d+\s+repl(?:y|ies))?\s*$/i,'').replace(/\s+Reply(?:\s+\d+)?(?:\s+View\s+\d+\s+repl(?:y|ies))?\s*$/i,'').replace(/\s+View\s+\d+\s+repl(?:y|ies)\s*$/i,'').replace(/\s+\d{1,2}-\d{1,2}\s*$/i,''));}
 function editors(root){return Array.from((root||document).querySelectorAll('textarea,[contenteditable="true"],div[contenteditable],[role="textbox"]')).filter(vis)}
 function panelState(){const W=window.innerWidth,H=window.innerHeight; const inView=el=>{if(!vis(el))return false; const r=el.getBoundingClientRect(); return r.y+r.height<=H&&r.x<W}; const shown=q=>Array.from(document.querySelectorAll(q)).some(vis); const comments=shown('[class*="DivCommentListContainer"],[class*="DivCommentContainer"],[class*="DivCommentInputWrapper"],[data-e2e="comment-input"]'); const buttons=Array.from(document.querySelectorAll('button')).filter(inView); const named=t=>buttons.filter(el=>clean(el.innerText||el.textContent)===t); const inBar=el=>Boolean(el.closest('.tux-tabbar__item-container,[class*="tabbar"]')); const topRight=el=>{const r=el.getBoundingClientRect(); return r.x>W*0.55&&r.y<220}; const pick=list=>list.find(inBar)||list.find(topRight)||null; const tab=pick(named('Comments')); const reco=pick(named('You may like')); const related=shown('[class*="DivRelatedTabContainer"]')||(Boolean(reco)&&!comments); const icon=Array.from(document.querySelectorAll('[data-e2e="comment-icon"]')).find(inView)||null; let count=null; const box=icon&&icon.parentElement?icon.parentElement.querySelector('[data-e2e="comment-count"]'):null; const m=box?clean(box.innerText||box.textContent).match(/^(\d+(?:[.,]\d+)?)([KkMm]?)$/):null; if(m){const s=m[2].toLowerCase(); count=Math.round(parseFloat(m[1].replace(',','.'))*(s==='k'?1000:(s==='m'?1000000:1)))} const state=comments?'comments':(related?'related':(tab?'unknown':'closed')); return {state:state,comments_tab:handle(tab),comment_icon:handle(icon),comment_count:count}}
 function videoAuthorHandles(){const zone='[data-e2e="browse-username"],[data-e2e="video-author-uniqueid"],[data-e2e="browse-user-avatar"],[data-e2e="video-author-avatar"],[class*="DivAuthorContainer"]'; const out='#app-header,nav,[data-e2e*="nav"],[class*="DivCommentItemWrapper"],[class*="DivReplyContainer"],[data-e2e^="comment-"],[class*="DivRelatedTabContainer"],[data-e2e="recommend-list-item-container"],[data-e2e="browse-video-desc"],[data-e2e="video-desc"],[data-e2e="search-common-link"]'; const sel='a[href*="/@"]'; const links=[]; for(const c of Array.from(document.querySelectorAll(zone))){if(c.closest(out))continue; const up=c.closest(sel); for(const a of (up?[up]:[]).concat(Array.from(c.querySelectorAll(sel)))){if(!links.includes(a))links.push(a)}} const seen=[]; for(const a of links){if(seen.length>=5)break; if(!vis(a)||a.closest(out))continue; const m=(a.getAttribute('href')||'').match(/\/@([^/?#]+)/); if(!m)continue; let h=''; try{h=decodeURIComponent(m[1])}catch(e){continue} h=h.replace(/^@+/,'').toLowerCase(); if(h&&!seen.includes(h))seen.push(h)} return seen}
"""

# s30 (console ADR 064, D2-D4, A1, A2, A6, A8): helpers of the DM path, injected into PROFILE_JS and THREAD_JS only
# (_HELPERS, shared with the reader of `tiktok-scan`, stays as it was). One definition per line ` function <name>(`
# (read by tests/js/dm_path.test.mjs). Read-only: no click, no event, no value written, no navigation.
# - shapeOf(el): the structure of an element for the `dm_probe` diagnostic — tag, data-e2e, role, the component name
#   of a `css-<hash>-<Name>` class, rectangle, in_view; never its text, its href nor its raw class.
# - messageButton(): the profile « Message » button — `[data-e2e="message-button"]`, or a BUTTON / [role=button] whose
#   own text is « Message » or whose aria-label holds the word Message; visible, OUT of the header and navigation
#   (the inbox link « Messages » of the navigation is never a candidate), nested candidates counted once, unique
#   required (else `ambiguous`); `excluded` = matching buttons seen in the header/navigation.
# - dmBlock(): a short visible leaf, out of the navigation, the profile bio/title and the user content (comments,
#   videos, bubbles, editors), saying DMs are refused.
# - dmEditor(): the editor of the conversation — visible editors out of the header, navigation, search and comment
#   field, nested ones counted once; one => it, several => the only one hinting « message », else none (ambiguous).
# - convColumn(ed) / convText(ed): the conversation column (highest ancestor of the editor whose left edge stays
#   within 80 px of the editor's) and its text without the editor's subtree (last 6000 characters, kept in memory).
# - sendButton(ed): the known send selectors inside that column, else a button whose data-e2e or aria-label says
#   send, on the editor's row (centres within 60 px) and right of the editor's left edge.
_DM_HELPERS = r"""
 function shapeOf(el){if(!el)return null; const r=el.getBoundingClientRect(); const raw=el.className&&el.className.baseVal!==undefined?el.className.baseVal:el.className; let cls=null; for(const c of String(raw||'').split(/\s+/)){const m=c.match(/^css-[0-9a-z]+-([A-Z][A-Za-z0-9]*)$/); if(m){cls=m[1];break}} return {tag:el.tagName||null,e2e:el.getAttribute('data-e2e'),role:el.getAttribute('role'),cls:cls,x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height),in_view:vis(el)&&r.y+r.height<=window.innerHeight&&r.x<window.innerWidth}}
 function messageButton(){const zone='#app-header,header,nav,[data-e2e*="nav"]'; const isBtn=el=>el.tagName==='BUTTON'||el.getAttribute('role')==='button'; const kind=el=>(el.getAttribute('data-e2e')||'')==='message-button'?'e2e':(isBtn(el)&&/^Message$/i.test(clean(el.innerText||el.textContent))?'text':(isBtn(el)&&/\bMessage\b/i.test(el.getAttribute('aria-label')||'')?'aria':null)); const found=Array.from(document.querySelectorAll('[data-e2e="message-button"],button,[role="button"]')).filter(el=>vis(el)&&kind(el)); const kept=found.filter(el=>!el.closest(zone)); const list=kept.filter(el=>!kept.some(o=>o!==el&&o.contains(el))); const el=list.length===1?list[0]:null; return {el:el,candidates:list.length,chosen:list.length===0?'none':(el?kind(el):'ambiguous'),excluded:found.length-kept.length,disabled:!!el&&(el.disabled===true||el.getAttribute('aria-disabled')==='true')}}
 function dmBlock(){const out='#app-header,header,nav,[data-e2e*="nav"],[data-e2e="user-bio"],[data-e2e="user-subtitle"],[data-e2e="user-title"],[data-e2e*="user-post-item"],[class*="DivCommentItemWrapper"],[class*="DivReplyContainer"],[data-e2e*="comment"],[data-e2e="chat-item"],[class*="DivChatItemWrapper"],textarea,[contenteditable],[role="textbox"]'; const re=/privacy|can.t send|cannot send|not accepting|follow each other|account is private/i; return Array.from(document.querySelectorAll('p,span,div')).some(el=>{if(el.children&&el.children.length)return false; if(!vis(el)||el.closest(out))return false; const t=clean(el.innerText||el.textContent); return t.length>0&&t.length<=120&&re.test(t)})}
 function dmEditor(){const out='#app-header,header,nav,[data-e2e*="nav"],[data-e2e*="search"],[class*="DivCommentInputWrapper"]'; const raw=editors().filter(e=>e.getAttribute('contenteditable')!=='false'&&!e.closest(out)); const all=raw.filter(e=>!raw.some(o=>o!==e&&o.contains(e))); const hint=e=>{const root=e.closest('.DraftEditor-root'); const ph=root?root.querySelector('.public-DraftEditorPlaceholder-root'):null; return /message/i.test([e.getAttribute('placeholder'),e.getAttribute('aria-placeholder'),e.getAttribute('aria-label'),ph?clean(ph.innerText||ph.textContent):''].join(' '))}; const hinted=all.filter(hint); const el=all.length===1?all[0]:(hinted.length===1?hinted[0]:null); return {el:el,all:all}}
 function convColumn(ed){if(!ed)return null; const ex=ed.getBoundingClientRect().x; let col=ed; for(let p=ed.parentElement;p&&p!==document.body&&p!==document.documentElement;p=p.parentElement){if(p.getBoundingClientRect().x<ex-80)break; col=p} return col}
 function convText(ed){const col=convColumn(ed); if(!col)return ''; const parts=[]; const walk=n=>{if(!n||n===ed)return; if(n.nodeType===3){parts.push(String(n.nodeValue||''));return} if(n.nodeType!==1)return; if(/^(BR|DIV|P|LI)$/.test(n.tagName||''))parts.push(' '); for(const c of Array.from(n.childNodes||[]))walk(c)}; walk(col); return clean(parts.join('')).slice(-6000)}
 function sendButton(ed){if(!ed)return null; const col=convColumn(ed); const inCol=Array.from(col.querySelectorAll('[data-e2e*="dm-new-send-btn"],[class*="dm-new-send-btn"],[aria-label*="Send" i]')).filter(vis); if(inCol.length)return inCol[0]; const er=ed.getBoundingClientRect(); const cy=er.y+er.height/2; return Array.from(document.querySelectorAll('button,[role="button"]')).filter(vis).find(b=>{const r=b.getBoundingClientRect(); return /send/i.test((b.getAttribute('data-e2e')||'')+' '+(b.getAttribute('aria-label')||''))&&Math.abs(r.y+r.height/2-cy)<=60&&r.x>=er.x})||null}
"""

# s28 (console ADR 062, D2): passive snapshot of the right panel of a video, read before any trusted click. `state` is
# read from the content (the tabs carry no state attribute): comments (list, input or comment container visible, the
# `panel` selectors of the reader guard) > related (« You may like » container, or its tab without a comment list) >
# unknown (a « Comments » tab, nothing else) > closed (no « Comments » tab). `comments_tab` = the « Comments » tab
# button, `comment_icon` = the comment icon fully inside the viewport (the next video of the feed has one below it),
# `comment_count` = the count of that icon's action block. No comment text, no URL, no handle in the result.
PANEL_STATE_JS = "(() => {" + _HELPERS + " return panelState();})()"

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

# s30 (console ADR 064, D1, D2, A6): profile page. `message_button` = the unique « Message » button of the profile out of
# the header and navigation (messageButton()), never a link; `disabled` = that button is disabled; `dm_block` = a
# structural « DM refused » wording (dmBlock()); `probe` = structure only, for the `dm_probe` diagnostic (counts,
# closed codes, shapeOf()): never a text, a handle nor an URL.
PROFILE_JS = (
    "(() => {"
    + _HELPERS
    + _DM_HELPERS
    + r"""
 const mb=messageButton();
 const navMsg=Array.from(document.querySelectorAll('a[href*="/messages"]')).filter(vis).length;
 const pathIsProfile=/^\/@[^/]+\/?$/.test(location.pathname);
 return {login_gate:loginGate(),path_is_profile:pathIsProfile,message_button:mb.el?handle(mb.el):null,disabled:mb.disabled,dm_block:dmBlock(),probe:{candidates:mb.candidates,chosen:mb.chosen,btn:shapeOf(mb.el),nav_msg_links:navMsg,excluded_btns:mb.excluded,path_is_profile:pathIsProfile}};
"""
    + "})()"
)

# DM thread (s30, console ADR 064, D3, D4, A2, A8): the editor of the conversation (dmEditor(), never the comment field),
# last outgoing bubbles (oldest first), send button of the editor's column (sendButton()), send error (l. 146-179),
# `conversation` = text of the conversation column without the editor (convText(), kept in memory, never printed),
# `probe` = structure only, for the `dm_probe` diagnostic.
THREAD_JS = (
    "(() => {"
    + _HELPERS
    + _DM_HELPERS
    + r"""
 const de=dmEditor(); const ed=de.el;
 const items=Array.from(document.querySelectorAll('[data-e2e="chat-item"], [class*="DivChatItemWrapper"]')).filter(vis);
 const box=(items[0]&&items[0].parentElement)?items[0].parentElement.getBoundingClientRect():{x:0,width:window.innerWidth};
 const mid=box.x+box.width/2;
 const outgoing=items.filter(it=>{const r=it.getBoundingClientRect(); return /my-?message|self/i.test(String(it.className||''))||r.x+r.width/2>mid}).map(it=>clean(it.innerText||it.textContent)).slice(-20);
 const send=sendButton(ed);
 const body=clean(document.body.innerText||'');
 const two=de.all.length>=2;
 return {login_gate:loginGate(),editor:ed?Object.assign(handle(ed),{text:clean(ed.innerText||ed.textContent||ed.value||'')}):null,outgoing,send_button:handle(send),error:/failed to send|couldn.t send|not sent|can.t send|cannot send|message could not be sent/i.test(body),conversation:ed?convText(ed):'',probe:{editors:de.all.length,ed:shapeOf(ed),ed1:two?shapeOf(de.all[0]):null,ed2:two?shapeOf(de.all[1]):null,path_messages:/^\/messages/.test(location.pathname),chat_items:items.length,send:shapeOf(send),dm_block:dmBlock()}};
"""
    + "})()"
)


def profile_url(handle: str) -> str:
    return PROFILE_URL.format(handle=handle)
