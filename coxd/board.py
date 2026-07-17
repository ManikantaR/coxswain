"""The board — a STATELESS reader over coxd's SQLite store (DESIGN-V35).

Unlike the old dashboard (which tailed log files and could show fiction), this
reads the one truthful store. If it dies, coxd keeps running; restart it and the
db is the truth. Starlette + SSE (deps already pulled in by the Agent SDK).

Run from coxd/:  .venv/bin/python board.py   (COXD_HOME selects the store)
"""

from __future__ import annotations

import asyncio
import json

import store
import uvicorn
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

STAGES = ["implement", "gate", "review", "merge"]
_STATE_STAGE = {"queued": 0, "provisioning": 0, "working": 0, "gating": 1,
                "fixing": 1, "reviewing": 2, "shipping": 3, "pr_ready": 3, "landed": 3}
_ACTIVE = {"provisioning", "working", "gating", "fixing", "reviewing", "shipping"}
_NEEDS_YOU = {"pr_ready", "needs_human"}


def _stage(state: str) -> dict:
    if state == "landed":
        return {"i": 3, "status": "done"}
    if state == "needs_human":
        return {"i": _STATE_STAGE.get("reviewing", 2), "status": "error"}
    return {"i": _STATE_STAGE.get(state, 0), "status": "active"}


def _tasks_payload() -> dict:
    rows = []
    for t in store.list_tasks():
        evs = store.events(t["id"])
        rows.append({
            "id": t["id"], "repo": t["repo"], "state": t["state"], "reason": t["reason"],
            "cost": t["cost"], "pr_url": t["pr_url"], "stage": _stage(t["state"]),
            "active": t["state"] in _ACTIVE, "needs_you": t["state"] in _NEEDS_YOU,
            "last": evs[-1]["kind"] + ": " + str(evs[-1]["data"])[:70] if evs else "",
        })
    rows.sort(key=lambda r: (not r["needs_you"], not r["active"], r["id"]))
    return {"tasks": rows, "stages": STAGES,
            "needs_you": sum(r["needs_you"] for r in rows),
            "active": sum(r["active"] for r in rows)}


async def index(_: Request) -> HTMLResponse:
    return HTMLResponse(_HTML)


async def api_tasks(_: Request) -> JSONResponse:
    return JSONResponse(_tasks_payload())


async def api_events(req: Request) -> JSONResponse:
    tid = req.path_params["tid"]
    return JSONResponse({"events": [
        {"kind": e["kind"], "data": e["data"]} for e in store.events(tid)][-40:]})


async def sse(_: Request) -> EventSourceResponse:
    async def gen():
        while True:
            yield {"data": json.dumps(_tasks_payload())}
            await asyncio.sleep(2)
    return EventSourceResponse(gen())


async def api_repos(_: Request) -> JSONResponse:
    import repos
    return JSONResponse({"repos": repos.list_repos()})


async def api_issues(req: Request) -> JSONResponse:
    import repos
    out = repos.list_issues(req.query_params.get("repo", ""))
    return JSONResponse(out, status_code=400 if out.get("error") else 200)


async def api_dispatch(req: Request) -> JSONResponse:
    import dispatch
    import repos
    body = await req.json()
    repo = str(body.get("repo") or "").strip()
    if not repo:
        return JSONResponse({"error": "pick a repo"}, status_code=400)
    brief = str(body.get("brief") or "").strip()
    issue = str(body.get("issue") or "").strip()
    if issue:
        info = repos.resolve_issue(issue, repo)
        if info.get("error"):
            return JSONResponse(info, status_code=400)
        block = f"{info['title']}\n\n{info['body']}\n\nGitHub issue: {info['url']}".strip()
        brief = f"{brief}\n\n{block}".strip() if brief else block
    if not brief:
        return JSONResponse({"error": "provide a GitHub issue or a brief"}, status_code=400)
    try:
        tid = dispatch.dispatch(repo, brief)
    except Exception as e:  # noqa: BLE001 - surface dispatch failures to the UI
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
    return JSONResponse({"id": tid})


app = Starlette(routes=[
    Route("/", index),
    Route("/api/tasks", api_tasks),
    Route("/api/task/{tid}/events", api_events),
    Route("/api/repos", api_repos),
    Route("/api/issues", api_issues),
    Route("/api/dispatch", api_dispatch, methods=["POST"]),
    Route("/events", sse),
])

_HTML = """<!doctype html><html><head><meta charset=utf-8><title>coxd</title>
<meta name=viewport content="width=device-width,initial-scale=1"><style>
:root{--bg:#0d1117;--panel:#161b22;--line:#2a3140;--fg:#e6edf3;--dim:#8b949e;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#3b82f6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:10px;align-items:center}
h1{font-size:15px;margin:0}.pill{font-size:12px;color:var(--dim);background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:3px 10px}
.pill b{color:var(--fg)}.spacer{flex:1}#conn{color:var(--dim)}main{padding:16px;max-width:820px;margin:0 auto;display:grid;gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}.card.needs{border-color:#5a4310}
.row{display:flex;gap:10px;align-items:center}.badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;text-transform:uppercase}
.b-work{background:#10233b;color:#6cb6ff}.b-warn{background:#2a2110;color:var(--warn)}.b-ok{background:#0f2417;color:var(--ok)}.b-bad{background:#2b1213;color:var(--bad)}
.tid{font-weight:600;word-break:break-all}.meta{color:var(--dim);font-size:12px;margin-top:2px}.last{font-size:12.5px;margin-top:6px;color:var(--fg);opacity:.85}
.pipe{display:flex;align-items:center;margin-top:10px}.step{display:flex;flex-direction:column;align-items:center;flex:0 0 auto;font-size:10px;text-transform:uppercase;color:#6e7681}
.dot{width:22px;height:22px;border-radius:50%;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-size:11px;margin-bottom:3px}
.step.done .dot{background:#0f2417;border-color:var(--ok);color:var(--ok)}.step.done{color:var(--ok)}
.step.active .dot{border-color:var(--acc);color:#6cb6ff;background:#10233b;animation:p 1.4s infinite}.step.active{color:#6cb6ff}
.step.error .dot{border-color:var(--bad);color:var(--bad);background:#2b1213}.step.error{color:var(--bad)}
.seg{flex:1;height:2px;background:var(--line);min-width:14px;margin-top:11px}.seg.on{background:var(--ok)}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(59,130,246,.5)}70%,100%{box-shadow:0 0 0 6px rgba(59,130,246,0)}}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--fg);border-radius:8px;padding:5px 11px;margin-top:10px}
.feed{margin-top:10px;border-top:1px solid var(--line);padding-top:8px;font-family:ui-monospace,monospace;font-size:12px;color:var(--dim);white-space:pre-wrap;display:none}
.feed.open{display:block}.empty{color:var(--dim);text-align:center;padding:40px}
#dp{border-bottom:1px solid var(--line);background:var(--panel)}.dform{max-width:820px;margin:0 auto;padding:14px 16px;display:grid;gap:8px}
input,select,textarea{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:7px 10px;width:100%}
textarea{resize:vertical}.drow{display:flex;gap:8px}.drow input{flex:1}#dgo.go{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:500}
#dmsg{font-size:13px;color:var(--dim);align-self:center}</style></head><body>
<header><h1>▟ coxd</h1><span class=pill id=pn>needs you <b>0</b></span><span class=pill id=pa>running <b>0</b></span>
<span class=spacer></span><button id=nb>+ Dispatch</button><span id=conn class=pill>connecting…</span></header>
<section id=dp hidden><div class=dform>
<select id=drepo><option value="">— pick a repo —</option></select>
<div class=drow><input id=diss placeholder="issue URL or # (auto-fills the brief)"><button id=dload>List issues</button></div>
<select id=disslist hidden></select>
<textarea id=dbrief rows=3 placeholder="brief (blank = use the issue)"></textarea>
<div class=drow><button id=dgo class=go>Dispatch</button><span id=dmsg></span></div>
</div></section>
<main id=board><div class=empty>Loading…</div></main>
<script>
const open=new Set(),esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));let STAGES=['implement','gate','review','merge'],sig=null;
const BADGE={queued:['b-work','queued'],provisioning:['b-work','provisioning'],working:['b-work','working'],gating:['b-work','gating'],fixing:['b-work','fixing'],reviewing:['b-work','reviewing'],shipping:['b-work','shipping'],pr_ready:['b-warn','ready to merge'],needs_human:['b-warn','needs you'],landed:['b-ok','landed']};
function pipe(st){let h='<div class=pipe>';for(let k=0;k<STAGES.length;k++){let c,ic;if(k<st.i){c='done';ic='✓'}else if(k===st.i){c=st.status==='done'?'done':st.status==='error'?'error':'active';ic=st.status==='error'?'✕':(c==='done'?'✓':k+1)}else{c='';ic=k+1}h+='<div class="step '+c+'"><span class=dot>'+ic+'</span>'+STAGES[k]+'</div>';if(k<STAGES.length-1)h+='<span class="seg'+(k<st.i?' on':'')+'"></span>'}return h+'</div>'}
async function loadFeed(id,el){const d=await(await fetch('/api/task/'+id+'/events')).json();el.textContent=(d.events||[]).map(e=>'· '+e.kind+' '+JSON.stringify(e.data).slice(0,90)).join('\\n')}
function render(s){STAGES=s.stages||STAGES;document.querySelector('#pn b').textContent=s.needs_you;document.querySelector('#pa b').textContent=s.active;
const k=JSON.stringify(s.tasks.map(t=>[t.id,t.state,t.reason,t.cost,t.last]));if(k===sig){for(const id of open){const el=document.querySelector('.card[data-id="'+CSS.escape(id)+'"] .feed');if(el)loadFeed(id,el)}return}sig=k;
const b=document.getElementById('board');if(!s.tasks.length){b.innerHTML='<div class=empty>No tasks yet.</div>';return}let h='';for(const t of s.tasks){const[cls,lab]=BADGE[t.state]||['b-work',t.state];
h+='<div class="card'+(t.needs_you?' needs':'')+'" data-id="'+esc(t.id)+'"><div class=row><span class="badge '+cls+'">'+lab+'</span><span class=tid>'+esc(t.id)+'</span></div>'+
'<div class=meta>'+esc(t.repo)+(t.cost?' · $'+t.cost.toFixed(3):'')+(t.reason?' · '+esc(t.reason):'')+(t.pr_url?' · <a href="'+esc(t.pr_url)+'" target=_blank style="color:#6cb6ff">PR</a>':'')+'</div>'+pipe(t.stage)+(t.last?'<div class=last>'+esc(t.last)+'</div>':'')+
'<button class=fb>'+(open.has(t.id)?'Hide':'Events')+'</button><div class="feed'+(open.has(t.id)?' open':'')+'"></div></div>'}
b.innerHTML=h;for(const c of b.querySelectorAll('.card')){const id=c.dataset.id,f=c.querySelector('.feed');if(open.has(id))loadFeed(id,f);
c.querySelector('.fb').onclick=()=>{if(open.has(id)){open.delete(id);f.classList.remove('open')}else{open.add(id);f.classList.add('open');loadFeed(id,f)}c.querySelector('.fb').textContent=open.has(id)?'Hide':'Events'}}}
const nb=document.getElementById('nb'),dp=document.getElementById('dp'),drepo=document.getElementById('drepo'),disslist=document.getElementById('disslist'),dmsg=document.getElementById('dmsg');
nb.onclick=()=>{dp.hidden=!dp.hidden;if(!dp.hidden)loadRepos()};
async function loadRepos(){try{const d=await(await fetch('/api/repos')).json();drepo.innerHTML='<option value="">— pick a repo —</option>'+(d.repos||[]).map(r=>'<option value="'+esc(r.path)+'">'+esc(r.name)+'</option>').join('')}catch(e){}}
document.getElementById('dload').onclick=async()=>{if(!drepo.value){dmsg.textContent='pick a repo';return}dmsg.textContent='loading issues…';try{const d=await(await fetch('/api/issues?repo='+encodeURIComponent(drepo.value))).json();if(d.error){dmsg.textContent='✗ '+d.error;return}if(!(d.issues||[]).length){dmsg.textContent='no open issues';disslist.hidden=true;return}disslist.innerHTML='<option value="">— pick an issue ('+d.issues.length+') —</option>'+d.issues.map(i=>'<option value="'+esc(i.url)+'">#'+i.number+' '+esc(i.title)+'</option>').join('');disslist.hidden=false;dmsg.textContent=''}catch(e){dmsg.textContent='✗ '+e}};
disslist.onchange=()=>{if(disslist.value)document.getElementById('diss').value=disslist.value};
document.getElementById('dgo').onclick=async()=>{const body={repo:drepo.value,issue:document.getElementById('diss').value.trim(),brief:document.getElementById('dbrief').value.trim()};if(!body.repo){dmsg.textContent='pick a repo';return}if(!body.issue&&!body.brief){dmsg.textContent='pick an issue or write a brief';return}dmsg.textContent='dispatching…';try{const d=await(await fetch('/api/dispatch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();if(d.error){dmsg.textContent='✗ '+d.error;return}dmsg.textContent='✓ queued '+d.id;document.getElementById('diss').value='';document.getElementById('dbrief').value='';disslist.hidden=true;sig=null}catch(e){dmsg.textContent='✗ '+e}};
const es=new EventSource('/events');es.onopen=()=>{const c=document.getElementById('conn');c.textContent='live'};es.onmessage=e=>render(JSON.parse(e.data));es.onerror=()=>{document.getElementById('conn').textContent='reconnecting…'};
fetch('/api/tasks').then(r=>r.json()).then(render).catch(()=>{});
</script></body></html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8791, log_level="warning")  # noqa: S104
