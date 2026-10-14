import base64
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = 'http://camofox:9377'
USER = 'hermes_80317d7dba'
DB = '/opt/data/tiktok-backoffice/tiktok_backoffice.sqlite3'
KANBAN = '/opt/data/kanban/boards/meta-campaigns/kanban.db'
PUBLIC_REPLY = 'Je viens de t’envoyer le lien en message privé'

CAMPAIGN_TITLES = {
    'markitdown': 'Campagne Markitdown - EP6',
    'higgsfield': 'Campagne Higgsfield - EP3',
    'voicebox': 'Campagne Voicebox - EP10',
    'graphify': 'Campagne Graphify - EP11',
    'proxy': 'Campagne Proxy - EP9',
    'gstack': 'Campagne Gstack - EP7',
    'obsidian': 'Campagne Obsidian - EP12',
    'competence-chatgpt': 'Campagne Competence - ChatGPT Skills',
    'ainnotations': 'Campagne Ainnotations - outil visuel',
}


def post(path, payload, timeout=60, method='POST'):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def eval_js(tab, expr, timeout=120):
    return post(f'/tabs/{tab}/evaluate', {'userId': USER, 'expression': expr}, timeout)['result']


def shell(cmd, timeout=60):
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=timeout)


def detect_display():
    cmd = "docker exec hermes-social-camofox-1 sh -lc \"ps -ef | sed -n 's/.*Xvfb \\(:[0-9][0-9]*\\).*/\\1/p' | tail -1\""
    return shell(cmd, timeout=20).stdout.strip() or ':99'


def get_review(review_id: int):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    row = con.execute('''
      SELECT ri.*, c.author, c.text AS comment_text, c.video_url, c.video_id
      FROM tiktok_review_items ri JOIN tiktok_comments c ON c.comment_id=ri.comment_id
      WHERE ri.id=?
    ''', (review_id,)).fetchone()
    if not row:
        raise SystemExit(f'review_not_found:{review_id}')
    return dict(row)


def get_dm_text(campaign_slug: str):
    title = CAMPAIGN_TITLES.get(campaign_slug)
    if not title:
        raise SystemExit(f'campaign_title_unknown:{campaign_slug}')
    con = sqlite3.connect(KANBAN); con.row_factory = sqlite3.Row
    row = con.execute('select body from tasks where title=? limit 1', (title,)).fetchone()
    if not row:
        raise SystemExit(f'kanban_campaign_not_found:{title}')
    m = re.search(r'Message DM:\s*(.*?)(?:\nURL ressource:|\nNotes:|\Z)', row['body'], flags=re.S)
    if not m:
        raise SystemExit(f'dm_text_not_found:{title}')
    return m.group(1).strip()


def update_status(review_id: int, status: str, reason: str, event: str, video_url: str):
    con = sqlite3.connect(DB)
    con.execute('''UPDATE tiktok_review_items SET status=?, failure_reason=?, updated_at=CURRENT_TIMESTAMP WHERE id=?''', (status, reason, review_id))
    con.execute('''INSERT INTO tiktok_browser_events(video_url,event_type,screenshot_path) VALUES(?,?,?)''', (video_url, event, None))
    con.commit()


def activate_comments(tab):
    expr = r'''
(async()=>{
 function clean(s){return String(s||'').replace(/\s+/g,' ').trim()}
 function vis(el){const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&r.x>=0&&r.y>=0}
 const els=Array.from(document.querySelectorAll('[data-e2e="comment-icon"],[aria-label*="comment" i],[aria-label*="Comments" i],button,[role=button],div,span')).filter(vis).map(el=>({el,text:clean(el.innerText||el.textContent),aria:el.getAttribute('aria-label')||'',e2e:el.getAttribute('data-e2e')||'',r:el.getBoundingClientRect()}));
 const c=els.find(x=>x.e2e==='comment-icon') || els.find(x=>/Read or add comments|comments?/i.test(x.aria) && x.r.x<1500) || els.find(x=>/^Comments$/i.test(x.text) && x.r.x>1000);
 if(c){ for(const t of ['mouseover','mousedown','mouseup','click']) c.el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})); }
 await new Promise(r=>setTimeout(r,2500));
 const body=clean(document.body.innerText||'');
 return {clicked:c?{text:c.text,aria:c.aria,e2e:c.e2e,rect:{x:Math.round(c.r.x),y:Math.round(c.r.y),w:Math.round(c.r.width),h:Math.round(c.r.height)}}:null, hasComments:/\b\d+ comments\b/i.test(body)||body.includes('Add comment'), bodyTail:body.slice(-1200)};
})()
'''
    return eval_js(tab, expr, timeout=90)


def video_guards_and_like(video_url, author, comment_text):
    tab = post('/tabs', {'userId': USER, 'sessionKey': f'guard_{author}_{int(time.time())}', 'url': video_url}, timeout=60)['tabId']
    time.sleep(8)
    activation = activate_comments(tab)
    expr = r'''
(async()=>{
 const AUTHOR=%s, COMMENT_TEXT=%s, OWNER='@dupflodev';
 function clean(s){return String(s||'').replace(/\s+/g,' ').trim()}
 function vis(el){const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&r.x>=0&&r.y>=0}
 function authorFrom(el){const links=Array.from(el.querySelectorAll('a[href^="/@"], a[href*="tiktok.com/@"]')); for(const a of links){const h=a.getAttribute('href')||''; const m=h.match(/\/@([^/?#]+)/); if(m)return '@'+decodeURIComponent(m[1]); const t=clean(a.innerText||a.textContent); if(t&&!t.includes(' '))return t.startsWith('@')?t:'@'+t} return null}
 function commentTextFrom(container){const content=container.querySelector('[class*="DivCommentContentWrapper"]')||container; const user=clean((content.querySelector('[data-e2e*="comment-username"], [class*="DivUsernameContentWrapper"]')||{}).innerText||''); let txt=clean(content.innerText||content.textContent); if(user&&txt.startsWith(user))txt=clean(txt.slice(user.length)); return clean(txt.replace(/\s+(?:\d+[smhdw]|\d+-\d+|Yesterday|Today)\s+ago\s+Reply(?:\s+\d+)?(?:\s+View\s+\d+\s+repl(?:y|ies))?\s*$/i,'').replace(/\s+Reply(?:\s+\d+)?(?:\s+View\s+\d+\s+repl(?:y|ies))?\s*$/i,'').replace(/\s+View\s+\d+\s+repl(?:y|ies)\s*$/i,'').replace(/\s+\d{1,2}-\d{1,2}\s*$/i,''));}
 function containers(){return Array.from(document.querySelectorAll('[class*="DivCommentItemWrapper"], [data-e2e*="comment"]')).filter(vis)}
 function targetNow(){return containers().find(el=>authorFrom(el)===AUTHOR && commentTextFrom(el).toLowerCase()===COMMENT_TEXT.toLowerCase()) || containers().find(el=>authorFrom(el)===AUTHOR && clean(el.innerText||el.textContent).toLowerCase().includes(COMMENT_TEXT.toLowerCase()))}
 const body=document.body.innerText||'';
 if(/\bLog\s*in\b|Sign\s*up|Log\s*in\s+to\s+comment/i.test(body)) return {ok:false,stage:'login_gate',url:location.href,title:document.title,body:body.slice(0,1000)};
 let target=targetNow();
 if(!target) return {ok:false,stage:'no_target',url:location.href,title:document.title,sample:containers().slice(0,25).map(el=>({author:authorFrom(el),text:commentTextFrom(el),y:Math.round(el.getBoundingClientRect().y)}))};
 target.scrollIntoView({block:'center',inline:'center'}); await new Promise(r=>setTimeout(r,800)); target=targetNow()||target;
 const tr=target.getBoundingClientRect();
 const viewBtns=Array.from(target.querySelectorAll('button,div,span,[role=button]')).filter(vis).map(el=>({el,text:clean(el.innerText||el.textContent),aria:el.getAttribute('aria-label')||'',r:el.getBoundingClientRect()})).filter(x=>/View\s+\d+\s+repl|View repl/i.test(x.text+x.aria));
 for(const b of viewBtns.slice(0,2)){ for(const t of ['mousedown','mouseup','click']) b.el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})); await new Promise(r=>setTimeout(r,700)); }
 const near=Array.from(document.querySelectorAll('[class*="DivCommentItemWrapper"], [data-e2e*="comment"], a, span, div')).filter(vis).map(el=>({author:authorFrom(el),text:clean(el.innerText||el.textContent),r:el.getBoundingClientRect()})).filter(x=>x.r.x>=1000&&x.r.y>=tr.y-20&&x.r.y<=tr.y+520);
 const ownerHits=near.filter(x=>x.author===OWNER || /flodev\s*·\s*Creator|Creator|dupflodev/i.test(x.text)).map(x=>({author:x.author,text:x.text.slice(0,260),rect:{x:Math.round(x.r.x),y:Math.round(x.r.y),w:Math.round(x.r.width),h:Math.round(x.r.height)}}));
 const alreadyReplied=ownerHits.some(x=>/envoy|lien|message privé|mp|dm|check tes dms/i.test(x.text));
 function likedState(el){const blob=(el.outerHTML+' '+(el.getAttribute('aria-pressed')||'')+' '+(el.getAttribute('aria-label')||'')).toLowerCase(); return blob.includes('aria-pressed="true"')||blob.includes('unlike')||blob.includes('liked')||blob.includes('ui-text-primary-display')}
 const btns=Array.from(target.querySelectorAll('button,[role=button],div,span,svg')).filter(vis).map(el=>({el,text:clean(el.innerText||el.textContent),aria:el.getAttribute('aria-label')||'',cls:String(el.className||''),r:el.getBoundingClientRect(),html:el.outerHTML.slice(0,400)}));
 const like=btns.filter(x=>/like/i.test(x.aria+x.cls+x.html) && x.r.x>tr.x+tr.width-80).sort((a,b)=>a.r.y-b.r.y)[0];
 let likeResult={found:false};
 if(like){const before=likedState(like.el); if(!before){for(const t of ['mousedown','mouseup','click']) like.el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})); await new Promise(r=>setTimeout(r,800));} likeResult={found:true,beforeLiked:before,afterLiked:likedState(like.el),aria:like.aria,text:like.text,rect:{x:Math.round(like.r.x),y:Math.round(like.r.y),w:Math.round(like.r.width),h:Math.round(like.r.height)}};}
 return {ok:true,stage:'guards_done',tabId:null,url:location.href,target:{author:authorFrom(target),text:commentTextFrom(target),rect:{x:Math.round(tr.x),y:Math.round(tr.y),w:Math.round(tr.width),h:Math.round(tr.height)}},alreadyReplied,ownerHits,like:likeResult};
})()
''' % (json.dumps(author), json.dumps(comment_text))
    result = eval_js(tab, expr, timeout=180)
    return tab, activation, result


def send_dm(profile, dm_text):
    profile_url = f'https://www.tiktok.com/{profile}'
    tab = post('/tabs', {'userId': USER, 'sessionKey': f'dm_{profile}_{int(time.time())}', 'url': profile_url}, timeout=60)['tabId']
    time.sleep(8)
    initial = eval_js(tab, r'''
(() => {function clean(s){return String(s||'').replace(/\s+/g,' ').trim()} function vis(el){const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&r.x>=0&&r.y>=0} const body=clean(document.body.innerText||''); const controls=Array.from(document.querySelectorAll('button,[role=button],a,div,span')).filter(vis).map(el=>({el,text:clean(el.innerText||el.textContent),aria:el.getAttribute('aria-label')||'',href:el.getAttribute('href')||'',r:el.getBoundingClientRect()})); const msg=controls.find(x=>/^Message$/i.test(x.text)||/\bMessage\b/i.test(x.aria)||/messages\?/i.test(x.href)); return {url:location.href,title:document.title,hasLogin:/\bLog\s*in\b|Sign\s*up/i.test(body),hasMessage:!!msg,messageButton:msg?{text:msg.text,aria:msg.aria,href:msg.href,rect:{x:Math.round(msg.r.x),y:Math.round(msg.r.y),w:Math.round(msg.r.width),h:Math.round(msg.r.height)}}:null,bodyPreview:body.slice(0,1200)}})()
''')
    if initial.get('hasLogin') or not initial.get('hasMessage'):
        return {'ok': False, 'stage': 'profile_no_message_or_login', 'tab': tab, 'initial': initial}
    clicked = eval_js(tab, r'''
(async()=>{function clean(s){return String(s||'').replace(/\s+/g,' ').trim()} function vis(el){const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&r.x>=0&&r.y>=0} const controls=Array.from(document.querySelectorAll('button,[role=button],a,div,span')).filter(vis).map(el=>({el,text:clean(el.innerText||el.textContent),aria:el.getAttribute('aria-label')||'',href:el.getAttribute('href')||'',r:el.getBoundingClientRect()})); const msg=controls.find(x=>/^Message$/i.test(x.text)||/\bMessage\b/i.test(x.aria)||/messages\?/i.test(x.href)); if(!msg)return {ok:false,stage:'no_message_button'}; for(const t of ['mouseover','mousedown','mouseup','click']) msg.el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})); await new Promise(r=>setTimeout(r,4000)); return {ok:true,stage:'clicked_message',href:msg.href,rect:{x:Math.round(msg.r.x),y:Math.round(msg.r.y),w:Math.round(msg.r.width),h:Math.round(msg.r.height)},url:location.href,title:document.title};})()
''', timeout=90)
    pre = eval_js(tab, r'''
(() => {const HANDLE=%s; const DISPLAY=%s; function clean(s){return String(s||'').replace(/\s+/g,' ').trim()} function vis(el){const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&r.x>=0&&r.y>=0} const body=clean(document.body.innerText||''); const lower=body.toLowerCase(); function scopedAlready(needles){const hits=[]; for(const needle of needles){let start=0,n=needle.toLowerCase(); while(true){const idx=lower.indexOf(n,start); if(idx<0)break; const context=body.slice(Math.max(0,idx-550),idx+800); const cl=context.toLowerCase(); const handleOk=HANDLE&&(cl.includes('@'+HANDLE.toLowerCase())||cl.includes(HANDLE.toLowerCase())); const displayOk=DISPLAY&&DISPLAY.length>=3&&cl.includes(DISPLAY.toLowerCase()); hits.push({needle,handleOk,displayOk,context}); start=idx+n.length;}} return hits.filter(h=>h.handleOk||h.displayOk);} const markitdownHits=scopedAlready(['ep=ep6','commenté « markitdown »','commenté « Markitdown »']); const higgsfieldHits=scopedAlready(['ep=ep3','commenté « higgsfield »','commenté « Higgsfield »']); const voiceboxHits=scopedAlready(['ep=ep10','commenté « voicebox »','commenté « Voicebox »']); const editors=Array.from(document.querySelectorAll('textarea,[contenteditable="true"],div[contenteditable],[role="textbox"],input')).filter(vis).map((el,i)=>{const r=el.getBoundingClientRect(); return {i,text:clean(el.innerText||el.textContent||el.value||''),placeholder:el.getAttribute('placeholder')||el.getAttribute('aria-placeholder')||el.getAttribute('aria-label')||'',rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}}}); return {url:location.href,title:document.title,handle:HANDLE,display:DISPLAY,editors,alreadyMarkitdown:markitdownHits.length>0,alreadyHiggsfield:higgsfieldHits.length>0,alreadyVoicebox:voiceboxHits.length>0,markitdownContext:markitdownHits[0]?.context||null,higgsfieldContext:higgsfieldHits[0]?.context||null,voiceboxContext:voiceboxHits[0]?.context||null,blocked:/privacy|can't send|cannot send|not accepting|follow each other/i.test(body)&&editors.length===0,bodyTail:body.slice(-1600)}})()
''' % (json.dumps(profile), json.dumps((initial.get('title') or '').split(' (@')[0].replace(' | TikTok','').strip())))
    if ('ep=ep6' in dm_text or 'Markitdown' in dm_text) and pre.get('alreadyMarkitdown'):
        return {'ok': True, 'stage': 'already_dm_sent', 'tab': tab, 'initial': initial, 'clicked': clicked, 'pre': pre}
    if ('ep=ep3' in dm_text or 'Higgsfield' in dm_text) and pre.get('alreadyHiggsfield'):
        return {'ok': True, 'stage': 'already_dm_sent', 'tab': tab, 'initial': initial, 'clicked': clicked, 'pre': pre}
    if ('ep=ep10' in dm_text or 'Voicebox' in dm_text) and pre.get('alreadyVoicebox'):
        return {'ok': True, 'stage': 'already_dm_sent', 'tab': tab, 'initial': initial, 'clicked': clicked, 'pre': pre}
    editors = pre.get('editors') or []
    if pre.get('blocked') or not editors:
        return {'ok': False, 'stage': 'dm_blocked_or_no_editor', 'tab': tab, 'initial': initial, 'clicked': clicked, 'pre': pre}
    focus = eval_js(tab, r'''
(() => {function vis(el){const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&r.x>=0&&r.y>=0} const editors=Array.from(document.querySelectorAll('textarea,[contenteditable="true"],div[contenteditable],[role="textbox"],input')).filter(vis); if(!editors.length)return {ok:false}; const el=editors[0]; el.focus(); const r=el.getBoundingClientRect(); return {ok:true,rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}}})()
''')
    display = detect_display()
    b64 = base64.b64encode(dm_text.encode()).decode()
    cmd = "docker exec hermes-social-camofox-1 sh -lc " + repr(f"export DISPLAY={display}; printf %s {b64!r} | base64 -d | xclip -selection clipboard; xdotool key ctrl+v")
    clip = shell(cmd, timeout=30)
    time.sleep(2)
    after_type = eval_js(tab, r'''
(() => {function clean(s){return String(s||'').replace(/\s+/g,' ').trim()} function vis(el){const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&r.x>=0&&r.y>=0} const body=clean(document.body.innerText||''); const editors=Array.from(document.querySelectorAll('textarea,[contenteditable="true"],div[contenteditable],[role="textbox"],input')).filter(vis).map(el=>{const r=el.getBoundingClientRect(); return {text:clean(el.innerText||el.textContent||el.value||''),placeholder:el.getAttribute('placeholder')||el.getAttribute('aria-placeholder')||el.getAttribute('aria-label')||'',rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}}}); const controls=Array.from(document.querySelectorAll('button,[role=button],div,span,svg')).filter(vis).map((el,i)=>({i,el,text:clean(el.innerText||el.textContent),aria:el.getAttribute('aria-label')||'',title:el.getAttribute('title')||'',cls:String(el.className||''),r:el.getBoundingClientRect(),html:el.outerHTML.slice(0,600)})); const sendCandidates=controls.filter(x=>/dm-new-send-btn|send/i.test(x.text+x.aria+x.title+x.cls+x.html)&&!/emoji/i.test(x.text+x.aria+x.title+x.cls+x.html)).filter(x=>/dm-new-send-btn/i.test(x.html+x.cls)||(x.r.width<=120&&x.r.height<=90&&x.r.x>800&&x.r.y>500)).sort((a,b)=>(/dm-new-send-btn/i.test(b.html+b.cls)?1:0)-(/dm-new-send-btn/i.test(a.html+a.cls)?1:0)).map(x=>({i:x.i,text:x.text,aria:x.aria,title:x.title,cls:x.cls.slice(0,120),rect:{x:Math.round(x.r.x),y:Math.round(x.r.y),w:Math.round(x.r.width),h:Math.round(x.r.height)},html:x.html})); return {url:location.href,editors,counterMatches:(body.match(/\d+\/6000/g)||[]).slice(-5),sendCandidates:sendCandidates.slice(0,8),bodyTail:body.slice(-1200)}})()
''')
    editor_text = (after_type.get('editors') or [{}])[0].get('text', '')
    counter_ok = any((s and not s.startswith('0/')) for s in after_type.get('counterMatches') or []) or ('http' in editor_text)
    if clip.returncode != 0 or not counter_ok or not after_type.get('sendCandidates'):
        return {'ok': False, 'stage': 'typed_but_send_not_ready', 'tab': tab, 'display': display, 'clip_rc': clip.returncode, 'clip_stderr': clip.stderr[-500:], 'initial': initial, 'clicked': clicked, 'pre': pre, 'focus': focus, 'after_type': after_type}
    send_click = eval_js(tab, r'''
(() => {function clean(s){return String(s||'').replace(/\s+/g,' ').trim()} function vis(el){const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&r.x>=0&&r.y>=0} const controls=Array.from(document.querySelectorAll('button,[role=button],div,span,svg')).filter(vis).map((el,i)=>({i,el,text:clean(el.innerText||el.textContent),aria:el.getAttribute('aria-label')||'',title:el.getAttribute('title')||'',cls:String(el.className||''),r:el.getBoundingClientRect(),html:el.outerHTML.slice(0,700)})); const c=controls.filter(x=>/dm-new-send-btn|send/i.test(x.text+x.aria+x.title+x.cls+x.html)&&!/emoji/i.test(x.text+x.aria+x.title+x.cls+x.html)).filter(x=>/dm-new-send-btn/i.test(x.html+x.cls)||(x.r.width<=120&&x.r.height<=90&&x.r.x>800&&x.r.y>500)).sort((a,b)=>(/dm-new-send-btn/i.test(b.html+b.cls)?1:0)-(/dm-new-send-btn/i.test(a.html+a.cls)?1:0))[0]; if(!c)return {ok:false,stage:'no_send_candidate'}; const target=(c.el.closest&&(c.el.closest('button')||c.el.closest('[role="button"]')))||c.el; const r=target.getBoundingClientRect(); for(const t of ['mouseover','mousedown','mouseup','click']) target.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})); return {ok:true,clickedTag:target.tagName,rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)},candidate:{text:c.text,aria:c.aria,cls:c.cls.slice(0,120),rect:{x:Math.round(c.r.x),y:Math.round(c.r.y),w:Math.round(c.r.width),h:Math.round(c.r.height)}}}})()
''')
    time.sleep(5)
    after_send = eval_js(tab, r'''
(() => {function clean(s){return String(s||'').replace(/\s+/g,' ').trim()} function vis(el){const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&r.x>=0&&r.y>=0} const body=clean(document.body.innerText||''); const editors=Array.from(document.querySelectorAll('textarea,[contenteditable="true"],div[contenteditable],[role="textbox"],input')).filter(vis).map(el=>{const r=el.getBoundingClientRect(); return {text:clean(el.innerText||el.textContent||el.value||''),placeholder:el.getAttribute('placeholder')||el.getAttribute('aria-placeholder')||el.getAttribute('aria-label')||'',rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}}}); const errorLike=/failed to send|couldn.t send|not sent|privacy|can.t send|cannot send|message could not be sent/i.test(body)||Array.from(document.querySelectorAll('svg,[aria-label],div,span')).some(el=>/exclamation|error|failed/i.test(String(el.getAttribute('aria-label')||'')+String(el.className||'')+String(el.outerHTML||'').slice(0,250))); return {url:location.href,title:document.title,editorEmpty:!(editors[0]&&editors[0].text),editors,errorLike,sentTextVisible:body.includes('Bonjour, tu reçois ce message')&&body.includes('http'),bodyTail:body.slice(-1600)}})()
''')
    ok = bool(after_send.get('editorEmpty') and after_send.get('sentTextVisible') and not after_send.get('errorLike'))
    return {'ok': ok, 'stage': 'sent_verified' if ok else 'sent_uncertain_or_failed', 'tab': tab, 'display': display, 'dm_len': len(dm_text), 'initial': initial, 'clicked': clicked, 'pre': pre, 'focus': focus, 'after_type': after_type, 'send_click': send_click, 'after_send': after_send}


def try_public_reply(tab, author, comment_text):
    expr = r'''
(async()=>{
 const AUTHOR=%s, COMMENT_TEXT=%s, REPLY=%s;
 function clean(s){return String(s||'').replace(/\s+/g,' ').trim()}
 function vis(el){const r=el.getBoundingClientRect(); return r.width>0&&r.height>0&&r.x>=0&&r.y>=0}
 function authorFrom(el){const links=Array.from(el.querySelectorAll('a[href^="/@"], a[href*="tiktok.com/@"]')); for(const a of links){const h=a.getAttribute('href')||''; const m=h.match(/\/@([^/?#]+)/); if(m)return '@'+decodeURIComponent(m[1]); const t=clean(a.innerText||a.textContent); if(t&&!t.includes(' '))return t.startsWith('@')?t:'@'+t} return null}
 function commentTextFrom(container){const content=container.querySelector('[class*="DivCommentContentWrapper"]')||container; const user=clean((content.querySelector('[data-e2e*="comment-username"], [class*="DivUsernameContentWrapper"]')||{}).innerText||''); let txt=clean(content.innerText||content.textContent); if(user&&txt.startsWith(user))txt=clean(txt.slice(user.length)); return clean(txt.replace(/\s+(?:\d+[smhdw]|\d+-\d+|Yesterday|Today)\s+ago\s+Reply(?:\s+\d+)?(?:\s+View\s+\d+\s+repl(?:y|ies))?\s*$/i,'').replace(/\s+Reply(?:\s+\d+)?(?:\s+View\s+\d+\s+repl(?:y|ies))?\s*$/i,'').replace(/\s+View\s+\d+\s+repl(?:y|ies)\s*$/i,'').replace(/\s+\d{1,2}-\d{1,2}\s*$/i,''));}
 function containers(){return Array.from(document.querySelectorAll('[class*="DivCommentItemWrapper"], [data-e2e*="comment"]')).filter(vis)}
 let target=containers().find(el=>authorFrom(el)===AUTHOR && commentTextFrom(el).toLowerCase()===COMMENT_TEXT.toLowerCase()) || containers().find(el=>authorFrom(el)===AUTHOR && clean(el.innerText||el.textContent).toLowerCase().includes(COMMENT_TEXT.toLowerCase()));
 if(!target)return {ok:false,stage:'no_target_for_public'};
 target.scrollIntoView({block:'center',inline:'center'}); await new Promise(r=>setTimeout(r,700));
 const tr=target.getBoundingClientRect();
 const replies=Array.from(target.querySelectorAll('button,div,span,[role=button]')).filter(vis).map(el=>({el,text:clean(el.innerText||el.textContent),aria:el.getAttribute('aria-label')||'',r:el.getBoundingClientRect(),cls:String(el.className||'')})).filter(x=>x.text==='Reply'||/reply/i.test(x.aria+x.cls));
 const replyBtn=replies.sort((a,b)=>a.r.y-b.r.y)[0];
 if(!replyBtn)return {ok:false,stage:'no_reply_button'};
 for(const t of ['mousedown','mouseup','click']) replyBtn.el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
 await new Promise(r=>setTimeout(r,1000));
 const allEditors=Array.from(document.querySelectorAll('textarea,[contenteditable="true"],div[contenteditable],[role="textbox"]')).filter(vis).map(el=>{const r=el.getBoundingClientRect(); let chain=[]; let n=el; for(let i=0;n&&i<7;i++,n=n.parentElement){chain.push({tag:n.tagName,cls:String(n.className||'').slice(0,90),text:clean(n.innerText||n.textContent).slice(0,120)})} return {el,text:clean(el.innerText||el.textContent||el.value||''),placeholder:el.getAttribute('placeholder')||el.getAttribute('aria-placeholder')||el.getAttribute('aria-label')||'',r,chain}});
 const editors=allEditors.filter(x=>/reply/i.test(x.placeholder) || /reply/i.test(x.chain.map(c=>c.text+' '+c.cls).join(' ')) || (x.r.y>tr.y && x.r.y<tr.y+260 && x.r.x>=tr.x-20));
 const editor=editors.sort((a,b)=>Math.abs(a.r.y-(tr.y+100))-Math.abs(b.r.y-(tr.y+100)))[0];
 if(!editor)return {ok:false,stage:'no_inline_reply_editor',target:{author:authorFrom(target),text:commentTextFrom(target),rect:{x:Math.round(tr.x),y:Math.round(tr.y),w:Math.round(tr.width),h:Math.round(tr.height)}},editors:allEditors.map(e=>({text:e.text,placeholder:e.placeholder,rect:{x:Math.round(e.r.x),y:Math.round(e.r.y),w:Math.round(e.r.width),h:Math.round(e.r.height)},chain:e.chain.slice(0,4)})).slice(0,8)};
 editor.el.focus(); if('value' in editor.el){editor.el.value=REPLY; editor.el.dispatchEvent(new Event('input',{bubbles:true}));} else {document.execCommand('selectAll',false,null); document.execCommand('insertText',false,REPLY); editor.el.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:REPLY}));}
 await new Promise(r=>setTimeout(r,600));
 const er=editor.el.getBoundingClientRect();
 const posts=Array.from(document.querySelectorAll('button,[role=button],div,span')).filter(vis).map(el=>({el,text:clean(el.innerText||el.textContent),aria:el.getAttribute('aria-label')||'',r:el.getBoundingClientRect(),cls:String(el.className||'')})).filter(x=>(/^Post$/i.test(x.text)||/post/i.test(x.aria+x.cls)) && x.r.x>=er.x && x.r.y>er.y-80 && x.r.y<er.y+160);
 const postBtn=posts.sort((a,b)=>Math.abs(a.r.y-er.y)-Math.abs(b.r.y-er.y))[0];
 const draft={text:clean(editor.el.innerText||editor.el.textContent||editor.el.value||''),placeholder:editor.placeholder,rect:{x:Math.round(er.x),y:Math.round(er.y),w:Math.round(er.width),h:Math.round(er.height)},postButtons:posts.map(x=>({text:x.text,aria:x.aria,rect:{x:Math.round(x.r.x),y:Math.round(x.r.y),w:Math.round(x.r.width),h:Math.round(x.r.height)}}))};
 if(draft.text!==REPLY || !postBtn)return {ok:false,stage:'draft_not_postable',target:{author:authorFrom(target),text:commentTextFrom(target)},draft};
 for(const t of ['mouseover','mousedown','mouseup','click']) postBtn.el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
 await new Promise(r=>setTimeout(r,2500));
 const body=clean(document.body.innerText||'');
 const hit=body.includes(REPLY) && /flodev|Creator|dupflodev/i.test(body);
 return {ok:hit,stage:hit?'public_reply_posted_verified':'public_reply_uncertain',target:{author:authorFrom(target),text:commentTextFrom(target)},draft,bodyTail:body.slice(-1200)};
})()
''' % (json.dumps(author), json.dumps(comment_text), json.dumps(PUBLIC_REPLY))
    return eval_js(tab, expr, timeout=180)


def main():
    review_id = int(sys.argv[1])
    r = get_review(review_id)
    dm_text = get_dm_text(r['campaign_slug'])
    video_tab, activation, guards = video_guards_and_like(r['video_url'], r['author'], r['comment_text'])
    result = {'review': {k: r[k] for k in ['id','campaign_slug','author','comment_text','video_url']}, 'video_tab': video_tab, 'activation': activation, 'guards': guards}
    if not guards.get('ok'):
        print(json.dumps({'ok': False, 'stage': 'guards_failed', **result}, ensure_ascii=False, indent=2)); return
    if guards.get('alreadyReplied'):
        update_status(review_id, 'ignored', 'already_replied_by_creator_detected_before_dm', f'already_public_replied:review_id={review_id} author={r["author"]}', r['video_url'])
        print(json.dumps({'ok': True, 'stage': 'ignored_already_replied', **result}, ensure_ascii=False, indent=2)); return
    dm = send_dm(r['author'], dm_text)
    result['dm'] = dm
    if not dm.get('ok'):
        update_status(review_id, 'needs_manual_public_fallback', f'dm_not_verified:{dm.get("stage")}', f'dm_not_verified:review_id={review_id} author={r["author"]} stage={dm.get("stage")}', r['video_url'])
        print(json.dumps({'ok': False, 'stage': 'dm_not_verified', **result}, ensure_ascii=False, indent=2)); return
    if dm.get('stage') == 'already_dm_sent':
        update_status(review_id, 'ignored', f'already_dm_sent_detected_for_{r["author"]}; no new DM sent', f'already_dm_sent:review_id={review_id} author={r["author"]}', r['video_url'])
        print(json.dumps({'ok': True, 'stage': 'ignored_already_dm_sent', **result}, ensure_ascii=False, indent=2)); return
    public = try_public_reply(video_tab, r['author'], r['comment_text'])
    result['public_reply'] = public
    public_note = public.get('stage') if isinstance(public, dict) else 'unknown'
    reason = f'dm_sent_verified_to_{r["author"]}; public_reply_stage={public_note}'
    status = 'posted'
    update_status(review_id, status, reason, f'dm_sent_verified:review_id={review_id} author={r["author"]} public_reply_stage={public_note}', r['video_url'])
    print(json.dumps({'ok': True, 'stage': 'processed', **result}, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
