"""The board — a STATELESS reader over coxd's SQLite store (DESIGN-V35).

Unlike the old dashboard (which tailed log files and could show fiction), this
reads the one truthful store. If it dies, coxd keeps running; restart it and the
db is the truth. Starlette + SSE (deps already pulled in by the Agent SDK).

Run from coxd/:  .venv/bin/python board.py   (COXD_HOME selects the store)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

import store

# Every dispatch brief in this project includes a line like
# "GitHub issue: https://github.com/<owner>/<repo>/issues/<n>" by convention —
# extract it so the board can link straight to the issue, not just show the title.
_ISSUE_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+")
import uvicorn
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

STAGES = ["implement", "gate", "review", "merge"]
_STATE_STAGE = {"queued": 0, "provisioning": 0, "working": 0, "gating": 1,
                "fixing": 1, "reviewing": 2, "shipping": 3, "ci-watching": 3,
                "pr_ready": 3, "landed": 3}
_ACTIVE = {"provisioning", "working", "gating", "fixing", "reviewing", "shipping",
          "ci-watching"}
_NEEDS_YOU = {"pr_ready", "needs_human"}
# Terminal, resolved states eligible for silent age-based archiving — never
# pr_ready/needs_human, those always need a human look regardless of age.
_ARCHIVABLE = {"landed", "failed"}
# Manual (explicit button/API) archiving is allowed from pr_ready/needs_human
# too — a human has already decided the task is dead (e.g. superseded, or its
# underlying issue closed via another path), so hiding it is a deliberate
# choice, not silent cleanup.
_MANUAL_ARCHIVABLE = _ARCHIVABLE | {"pr_ready", "needs_human"}
_ARCHIVE_DAYS = float(os.environ.get("COXD_ARCHIVE_DAYS", "14"))


# Maps a needs_human `reason` to the stage it actually failed at — previously
# every needs_human task showed at "review" regardless of where it really failed
# (a worker-error during implement looked identical to a real review rejection).
_REASON_STAGE = {
    "worker-error": 0, "coxd-error": 0,
    "gate-red": 1,
    "review-error": 2, "review-findings": 2,
    "push-rejected": 3, "pr-error": 3,
}


def _reason_stage(reason: str | None) -> int:
    if not reason:
        return 2  # unknown reason — review is the least-wrong default
    if reason.startswith("ci-red"):
        return 3
    return _REASON_STAGE.get(reason, 2)


def _stage(state: str, reason: str | None = None) -> dict:
    if state == "landed":
        return {"i": 3, "status": "done"}
    if state == "needs_human":
        return {"i": _reason_stage(reason), "status": "error"}
    return {"i": _STATE_STAGE.get(state, 0), "status": "active"}


def _is_archived(t: dict) -> bool:
    if t.get("archived_at"):  # manually archived via the board's per-tile button
        return True
    if t["state"] not in _ARCHIVABLE:
        return False
    age_days = (time.time() - (t["updated"] or t["created"] or 0)) / 86400
    return age_days > _ARCHIVE_DAYS


def _tasks_payload(show_archived: bool = False) -> dict:
    rows = []
    archived_count = 0
    for t in store.list_tasks():
        if _is_archived(t):
            archived_count += 1
            if not show_archived:
                continue
        evs = store.events(t["id"])
        issue_match = _ISSUE_URL_RE.search(t.get("brief") or "")
        rows.append({
            "id": t["id"], "repo": t["repo"], "state": t["state"], "reason": t["reason"],
            "cost": t["cost"], "pr_url": t["pr_url"], "stage": _stage(t["state"], t["reason"]),
            "active": t["state"] in _ACTIVE, "needs_you": t["state"] in _NEEDS_YOU,
            "issue_url": issue_match.group(0) if issue_match else None,
            "archivable": t["state"] in _MANUAL_ARCHIVABLE,
            "archived": bool(t.get("archived_at")),
            "last": evs[-1]["kind"] + ": " + str(evs[-1]["data"])[:70] if evs else "",
            "_updated": t["updated"] or t["created"] or 0,
        })
    # needs-you first, then active, then most-recently-updated first within each
    # tier — previously fell back to alphabetical-by-id, which isn't chronological
    # (task ids are name-slug prefixed, only suffixed with a timestamp).
    rows.sort(key=lambda r: (not r["needs_you"], not r["active"], -r["_updated"]))
    for r in rows:
        del r["_updated"]
    return {"tasks": rows, "stages": STAGES,
            "needs_you": sum(r["needs_you"] for r in rows),
            "active": sum(r["active"] for r in rows),
            "archived": archived_count, "archive_days": _ARCHIVE_DAYS}


async def index(_: Request) -> HTMLResponse:
    return HTMLResponse(_HTML)


async def api_tasks(req: Request) -> JSONResponse:
    return JSONResponse(_tasks_payload(show_archived=req.query_params.get("all") == "1"))


async def api_events(req: Request) -> JSONResponse:
    tid = req.path_params["tid"]
    return JSONResponse({"events": [
        {"kind": e["kind"], "data": e["data"], "ts": e["ts"]}
        for e in store.events(tid)][-60:]})


async def api_task_retry(req: Request) -> JSONResponse:
    """Re-queue a needs_human task from scratch — same as the manual `UPDATE tasks
    SET state='queued'` this board grew out of doing by hand. Only sane before any
    commit landed in the worktree (e.g. worker-error/auth-error); for a task with
    real committed work already, prefer /resume so the existing session's context
    isn't thrown away."""
    tid = req.path_params["tid"]
    t = store.get_task(tid)
    if not t:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    if t["state"] != "needs_human":
        return JSONResponse({"error": f"cannot retry from state '{t['state']}'"}, status_code=400)
    store.set_state(tid, "queued", None)
    return JSONResponse({"ok": True})


async def api_task_stop(req: Request) -> JSONResponse:
    """Cancel an in-flight task in place — the missing option before today (see
    2026-07-27: a mistaken /retry on a task that already had a green PR raced
    to completion and opened a duplicate PR, with no way to interrupt it short
    of merging the original PR first). Only valid while actually running;
    lands at needs_human/stopped-by-user, same unstick path (/resume) as any
    other needs_human task — whatever was already committed is not discarded."""
    tid = req.path_params["tid"]
    t = store.get_task(tid)
    if not t:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    if t["state"] not in _ACTIVE:
        return JSONResponse({"error": f"cannot stop from state '{t['state']}'"}, status_code=400)
    import supervisor
    if not supervisor.cancel(tid):
        return JSONResponse({"error": "task isn't actually running (already finishing?)"},
                            status_code=409)
    return JSONResponse({"ok": True})


async def api_task_resume(req: Request) -> JSONResponse:
    """Resume the SAME worker session with a human-provided note, then re-run the
    shared gate->review->ship tail. For a needs_human task that already has real
    committed work — a targeted unstick, not a from-scratch retry."""
    tid = req.path_params["tid"]
    body = await req.json()
    note = str(body.get("note") or "").strip()
    if not note:
        return JSONResponse({"error": "note is required"}, status_code=400)
    t = store.get_task(tid)
    if not t:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    if t["state"] != "needs_human":
        return JSONResponse({"error": f"cannot resume from state '{t['state']}'"}, status_code=400)
    if not t["session_id"]:
        return JSONResponse({"error": "no prior session — use retry instead"}, status_code=400)
    import loop
    store.set_state(tid, "fixing")  # claim immediately so the board reflects it's active
    asyncio.create_task(loop.resume_task(tid, note))
    return JSONResponse({"ok": True})


async def api_task_archive(req: Request) -> JSONResponse:
    tid = req.path_params["tid"]
    t = store.get_task(tid)
    if not t:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    if t["state"] not in _MANUAL_ARCHIVABLE:
        return JSONResponse(
            {"error": f"cannot archive from state '{t['state']}'"},
            status_code=400,
        )
    store.archive_task(tid)
    return JSONResponse({"ok": True})


async def api_task_unarchive(req: Request) -> JSONResponse:
    tid = req.path_params["tid"]
    t = store.get_task(tid)
    if not t:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    store.unarchive_task(tid)
    return JSONResponse({"ok": True})


async def api_restart(_: Request) -> JSONResponse:
    """Restart THIS supervisor process in place (os.execv — same PID, fresh code
    and fresh credentials from disk). Refuses while any task is active: an
    in-flight SDK session would be orphaned mid-call, exactly what happened
    tonight requiring a manual worktree inspection + re-queue afterward."""
    payload = _tasks_payload()
    if payload["active"] > 0:
        return JSONResponse(
            {"error": f"{payload['active']} task(s) still active — wait for them "
                      "to finish or they'll be orphaned mid-session"}, status_code=409)
    import os
    import sys
    asyncio.get_event_loop().call_later(
        0.3, lambda: os.execv(sys.executable, [sys.executable] + sys.argv))
    return JSONResponse({"ok": True, "restarting": True})


async def sse(req: Request) -> EventSourceResponse:
    show_archived = req.query_params.get("all") == "1"

    async def gen():
        while True:
            yield {"data": json.dumps(_tasks_payload(show_archived=show_archived))}
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
    from pathlib import Path
    if not Path(repo).is_dir():
        return JSONResponse(
            {"error": f"not a repo path: {repo!r} — use the full path from /api/repos, not the short name"},
            status_code=400,
        )
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


async def api_suggestions(_: Request) -> JSONResponse:
    """Pending machine-discovered candidate rules (issue #7) — session_miner.py
    writes these; the board is the only path that turns one into a durable rule
    (captain-gated, same trust boundary as the old manual promote_rule)."""
    return JSONResponse({"suggestions": store.list_rule_suggestions(status="pending")})


async def api_suggestion_approve(req: Request) -> JSONResponse:
    import rules
    sid = int(req.path_params["sid"])
    s = store.get_rule_suggestion(sid)
    if not s:
        return JSONResponse({"error": "unknown suggestion"}, status_code=404)
    if s["status"] != "pending":
        return JSONResponse({"error": f"already {s['status']}"}, status_code=400)
    added = rules.add_rule(s["repo"], s["text"])
    store.decide_rule_suggestion(sid, "approved")
    return JSONResponse({"ok": True, "added": added})


async def api_suggestion_reject(req: Request) -> JSONResponse:
    sid = int(req.path_params["sid"])
    s = store.get_rule_suggestion(sid)
    if not s:
        return JSONResponse({"error": "unknown suggestion"}, status_code=404)
    if s["status"] != "pending":
        return JSONResponse({"error": f"already {s['status']}"}, status_code=400)
    store.decide_rule_suggestion(sid, "rejected")
    return JSONResponse({"ok": True})


app = Starlette(routes=[
    Route("/", index),
    Route("/api/tasks", api_tasks),
    Route("/api/task/{tid}/events", api_events),
    Route("/api/repos", api_repos),
    Route("/api/issues", api_issues),
    Route("/api/dispatch", api_dispatch, methods=["POST"]),
    Route("/api/task/{tid}/retry", api_task_retry, methods=["POST"]),
    Route("/api/task/{tid}/stop", api_task_stop, methods=["POST"]),
    Route("/api/task/{tid}/resume", api_task_resume, methods=["POST"]),
    Route("/api/task/{tid}/archive", api_task_archive, methods=["POST"]),
    Route("/api/task/{tid}/unarchive", api_task_unarchive, methods=["POST"]),
    Route("/api/restart", api_restart, methods=["POST"]),
    Route("/api/suggestions", api_suggestions),
    Route("/api/suggestion/{sid}/approve", api_suggestion_approve, methods=["POST"]),
    Route("/api/suggestion/{sid}/reject", api_suggestion_reject, methods=["POST"]),
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
.retryrow{display:flex;gap:6px;align-items:center;margin-top:8px;flex-wrap:wrap}.retryrow button{margin-top:0}.retryrow input{flex:1;min-width:160px;padding:5px 8px}.rsmsg{font-size:12px;color:var(--dim)}
.pipe{display:flex;align-items:center;margin-top:10px}.step{display:flex;flex-direction:column;align-items:center;flex:0 0 auto;font-size:10px;text-transform:uppercase;color:#6e7681;cursor:pointer;padding:2px 4px;border-radius:6px;transition:background .15s}.step:hover{background:#1c2333}
.dot{width:22px;height:22px;border-radius:50%;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-size:11px;margin-bottom:3px;transition:background .3s,border-color .3s,color .3s,transform .3s}
.step.done .dot{background:#0f2417;border-color:var(--ok);color:var(--ok);animation:pop .35s ease}.step.done{color:var(--ok)}
.step.active .dot{border-color:var(--acc);color:#6cb6ff;background:#10233b;animation:p 1.4s infinite}.step.active{color:#6cb6ff}
.step.error .dot{border-color:var(--bad);color:var(--bad);background:#2b1213;animation:shake .4s ease}.step.error{color:var(--bad)}
.seg{flex:1;height:2px;background:var(--line);min-width:14px;margin-top:11px;position:relative;overflow:hidden}
.seg.on::after{content:'';position:absolute;inset:0;background:var(--ok);animation:fill .5s ease forwards}
.card{animation:cardIn .25s ease}.card.flash{animation:flash 1s ease}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(59,130,246,.5)}70%,100%{box-shadow:0 0 0 6px rgba(59,130,246,0)}}
@keyframes pop{0%{transform:scale(.6)}60%{transform:scale(1.15)}100%{transform:scale(1)}}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-3px)}75%{transform:translateX(3px)}}
@keyframes fill{from{transform:translateX(-100%)}to{transform:translateX(0)}}
@keyframes cardIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
@keyframes flash{0%{background:#1c2b1c}100%{background:var(--panel)}}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--fg);border-radius:8px;padding:5px 11px;margin-top:10px}
.feed{margin-top:10px;border-top:1px solid var(--line);padding-top:8px;font-family:ui-monospace,monospace;font-size:12px;color:var(--dim);white-space:pre-wrap;display:none}
.feed.open{display:block}.empty{color:var(--dim);text-align:center;padding:40px}
#dp{border-bottom:1px solid var(--line);background:var(--panel)}.dform{max-width:820px;margin:0 auto;padding:14px 16px;display:grid;gap:8px}
input,select,textarea{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:7px 10px;width:100%}
textarea{resize:vertical}.drow{display:flex;gap:8px}.drow input{flex:1}#dgo.go{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:500}
#dmsg{font-size:13px;color:var(--dim);align-self:center}</style></head><body>
<header><h1>▟ coxd</h1><span class=pill id=pn>needs you <b>0</b></span><span class=pill id=pa>running <b>0</b></span>
<button id=ab class=pill hidden>archived <b>0</b></button>
<span class=spacer></span><button id=rb title="Restart the coxd process in place — refuses while anything is active">↻ Restart cox</button><button id=nb>+ Dispatch</button><span id=conn class=pill>connecting…</span></header>
<section id=dp hidden><div class=dform>
<select id=drepo><option value="">— pick a repo —</option></select>
<div class=drow><input id=diss placeholder="issue URL or # (auto-fills the brief)"><button id=dload>List issues</button></div>
<select id=disslist hidden></select>
<textarea id=dbrief rows=3 placeholder="brief (blank = use the issue)"></textarea>
<div class=drow><button id=dgo class=go>Dispatch</button><span id=dmsg></span></div>
</div></section>
<section id=sug hidden><div class=dform>
<div class=row><b>Suggested rules</b><span class=spacer></span><button id=sugtoggle>Hide</button></div>
<div id=suglist></div>
</div></section>
<main id=board><div class=empty>Loading…</div></main>
<script>
const open=new Set(),esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));let STAGES=['implement','gate','review','merge'],sig=null;
const STAGE_TIP=[
 'IMPLEMENT — the worker (Claude) writes code in an isolated git worktree and commits. It never pushes; the control plane does that later.',
 'GATE — deterministic checks (migrations, tests, lint, build) run against the repo\\'s real toolchain. Must pass before the paid review runs. A failure here resumes the SAME worker session with the exact error to fix, up to 2 rounds.',
 'REVIEW — one pass from a second model (Opus) against the full diff since the branch point. Checks correctness/security/contract; verdict is approve, fix (resumes the worker once), or reject (stops for you).',
 'MERGE — pushes the branch, opens the PR, then watches its CI checks: known flakes get auto-retried (up to 2x), a real-looking failure or one touching e2e/test files stops for you. Passing CI still waits for YOUR merge click — that\\'s the one standing human gate.',
];
const BADGE={queued:['b-work','queued'],provisioning:['b-work','provisioning'],working:['b-work','working'],gating:['b-work','gating'],fixing:['b-work','fixing'],reviewing:['b-work','reviewing'],shipping:['b-work','shipping'],'ci-watching':['b-work','watching CI'],pr_ready:['b-warn','ready to merge'],needs_human:['b-warn','needs you'],landed:['b-ok','landed']};
function pipe(st){let h='<div class=pipe>';for(let k=0;k<STAGES.length;k++){let c,ic;if(k<st.i){c='done';ic='✓'}else if(k===st.i){c=st.status==='done'?'done':st.status==='error'?'error':'active';ic=st.status==='error'?'✕':(c==='done'?'✓':k+1)}else{c='';ic=k+1}
h+='<div class="step '+c+'" data-stage="'+k+'" title="'+esc(STAGE_TIP[k])+'"><span class=dot>'+ic+'</span>'+STAGES[k]+'</div>';if(k<STAGES.length-1)h+='<span class="seg'+(k<st.i?' on':'')+'"></span>'}return h+'</div>'}
function fmtTime(ts){if(!ts)return'--:--:--';return new Date(ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function fmtFindings(fs){if(!fs||!fs.length)return'    (none)';return fs.map(f=>'    - ['+(f.severity||'?')+'/'+(f.pillar||'?')+'] '+(f.file?f.file+(f.line?':'+f.line:'')+' — ':'')+(f.summary||'')).join('\\n')}
function feedLine(e,t){
if(e.kind==='state'){const st=String((e.data||{}).state||'').toUpperCase(),rs=(e.data||{}).reason;return'\\n'+t+'▸ '+st+(rs?' — '+rs:'')}
if(e.kind==='gate'){const g=e.data||{};if(g.passed)return t+'  ✓ gate passed'+(g.steps?' ('+Object.entries(g.steps).map(([k,v])=>k+':'+v).join(', ')+')':'');
return t+'  ✕ gate FAILED at '+g.failing+(g.reason?'\\n      reason: '+String(g.reason).slice(0,600).split('\\n').join('\\n      '):'')}
if(e.kind==='review'){const r=e.data||{};return t+'  review — outcome:'+r.outcome+' verdict:'+r.verdict+'\\n'+fmtFindings(r.findings)}
if(e.kind==='fix-trigger'){const f=e.data||{};return t+'  → sending back to fix (source: '+f.source+')\\n'+
  (f.source==='gate'?'    '+f.failing+': '+String(f.reason||'').slice(0,400).split('\\n').join('\\n    '):fmtFindings(f.findings))}
return t+'    '+e.kind+' '+JSON.stringify(e.data).slice(0,90)}
const prevState=new Map();
const STATE_TO_STAGE={provisioning:0,working:0,fixing:0,gating:1,reviewing:2,shipping:3,'ci-watching':3};
function tagStages(events){let cur=0;return events.map(e=>{if(e.kind==='state'){const s=(e.data||{}).state;if(s in STATE_TO_STAGE)cur=STATE_TO_STAGE[s]}return Object.assign({},e,{_stage:cur})})}
const feedFilter=new Map();
async function loadFeed(id,el){const d=await(await fetch('/api/task/'+id+'/events')).json();
let evs=tagStages(d.events||[]);const fs=feedFilter.get(id);
let head='';if(fs!=null){evs=evs.filter(e=>e._stage===fs);head='── showing '+STAGES[fs].toUpperCase()+' only — click that step again to show everything ──\\n\\n'}
el.textContent=head+evs.map(e=>feedLine(e,'['+fmtTime(e.ts)+'] ')).join('\\n').replace(/^\\n/,'')}
function render(s){STAGES=s.stages||STAGES;document.querySelector('#pn b').textContent=s.needs_you;document.querySelector('#pa b').textContent=s.active;
const ab=document.getElementById('ab');if(s.archived){ab.hidden=false;ab.querySelector('b').textContent=s.archived;ab.title='landed/failed tasks older than '+s.archive_days+' days ('+(showArchived?'shown':'hidden')+')'}else{ab.hidden=true}
const k=JSON.stringify(s.tasks.map(t=>[t.id,t.state,t.reason,t.cost,t.last,t.issue_url]));if(k===sig){for(const id of open){const el=document.querySelector('.card[data-id="'+CSS.escape(id)+'"] .feed');if(el)loadFeed(id,el)}return}sig=k;
const changed=new Set();for(const t of s.tasks){if(prevState.has(t.id)&&prevState.get(t.id)!==t.state)changed.add(t.id);prevState.set(t.id,t.state)}
const b=document.getElementById('board');if(!s.tasks.length){b.innerHTML='<div class=empty>No tasks yet.</div>';return}let h='';for(const t of s.tasks){const[cls,lab]=BADGE[t.state]||['b-work',t.state];
const issueNum=t.issue_url?t.issue_url.split('/').pop():null;
h+='<div class="card'+(t.needs_you?' needs':'')+(changed.has(t.id)?' flash':'')+'" data-id="'+esc(t.id)+'"><div class=row><span class="badge '+cls+'">'+lab+'</span><span class=tid>'+esc(t.id)+'</span>'+
(t.issue_url?'<a href="'+esc(t.issue_url)+'" target=_blank style="color:#6cb6ff;font-size:12px">#'+esc(issueNum)+'</a>':'')+'</div>'+
'<div class=meta>'+esc(t.repo)+(t.cost?' · $'+t.cost.toFixed(3):'')+(t.reason?' · '+esc(t.reason):'')+(t.pr_url?' · <a href="'+esc(t.pr_url)+'" target=_blank style="color:#6cb6ff">PR</a>':'')+'</div>'+pipe(t.stage)+(t.last?'<div class=last>'+esc(t.last)+'</div>':'')+
(t.active?'<div class=retryrow><button class=spb data-pr="'+(t.pr_url?'1':'')+'" title="Cancel this task in place. Whatever is already committed is kept — lands at needs_human, same as any other stall, and Resume w/ note picks it back up.">Stop</button></div>':'')+
(t.state==='needs_human'?'<div class=retryrow>'+
 '<button class=rtb data-pr="'+(t.pr_url?'1':'')+'" title="Starts over from the original brief in a fresh worker call — discards this session\\'s context. Safe when nothing was committed yet (e.g. an auth/infra error before any real work happened).">Retry fresh</button>'+
 '<button class=rsb title="Keeps the existing session and any commits already made, and gives it your note as the next instruction. Cheaper and more targeted than Retry when real work already landed.">Resume w/ note</button>'+
 '<input class=rsnote hidden placeholder="what should it try differently?"><button class=rsgo hidden>Go</button><span class=rsmsg></span></div>':'')+
(t.archived?'<button class=arb data-mode=unarchive title="Bring this task back into the normal board view.">Unarchive</button>':t.archivable?'<button class=arb data-mode=archive title="Hide this completed task from the board now — same as it aging out on its own, just immediate.">Archive</button>':'')+
'<button class=fb>'+(open.has(t.id)?'Hide':'Events')+'</button><div class="feed'+(open.has(t.id)?' open':'')+'"></div></div>'}
b.innerHTML=h;for(const c of b.querySelectorAll('.card')){const id=c.dataset.id,f=c.querySelector('.feed');if(open.has(id))loadFeed(id,f);
c.querySelector('.fb').onclick=()=>{if(open.has(id)){open.delete(id);f.classList.remove('open')}else{open.add(id);f.classList.add('open');loadFeed(id,f)}c.querySelector('.fb').textContent=open.has(id)?'Hide':'Events'};
const arb=c.querySelector('.arb');if(arb)arb.onclick=async()=>{arb.disabled=true;try{const r=await(await fetch('/api/task/'+id+'/'+arb.dataset.mode,{method:'POST'})).json();if(r.error){alert(r.error);arb.disabled=false}else{sig=null}}catch(e){alert(e);arb.disabled=false}};
c.querySelectorAll('.step').forEach(stepEl=>{stepEl.onclick=()=>{const s=parseInt(stepEl.dataset.stage,10);
feedFilter.set(id,feedFilter.get(id)===s?undefined:s);open.add(id);f.classList.add('open');c.querySelector('.fb').textContent='Hide';loadFeed(id,f)}});
const spb=c.querySelector('.spb');if(spb)spb.onclick=async()=>{if(!confirm('Stop this task now? Whatever is already committed is kept — it lands at needs_human and you can Resume w/ note to pick it back up.'))return;spb.disabled=true;spb.textContent='stopping…';try{const r=await(await fetch('/api/task/'+id+'/stop',{method:'POST'})).json();if(r.error){alert(r.error);spb.disabled=false;spb.textContent='Stop'}else{sig=null}}catch(e){alert(e)}};
const rtb=c.querySelector('.rtb');if(rtb)rtb.onclick=async()=>{const warn=rtb.dataset.pr?'This task already has an open PR — Retry fresh starts a SECOND implementation from scratch and will likely open a duplicate PR. Merge or close the existing PR first, or use Resume w/ note instead. Continue anyway?':'Retry fresh discards this session\\'s context and starts over from the original brief. Continue?';if(!confirm(warn))return;rtb.disabled=true;rtb.textContent='retrying…';try{const r=await(await fetch('/api/task/'+id+'/retry',{method:'POST'})).json();if(r.error){alert(r.error);rtb.disabled=false;rtb.textContent='Retry fresh'}else{sig=null}}catch(e){alert(e)}};
const rsb=c.querySelector('.rsb'),rsnote=c.querySelector('.rsnote'),rsgo=c.querySelector('.rsgo'),rsmsg=c.querySelector('.rsmsg');
if(rsb)rsb.onclick=()=>{rsb.hidden=true;rsnote.hidden=false;rsgo.hidden=false;rsnote.focus()};
if(rsgo)rsgo.onclick=async()=>{const note=rsnote.value.trim();if(!note){rsmsg.textContent='note required';return}rsgo.disabled=true;rsmsg.textContent='resuming…';
try{const r=await(await fetch('/api/task/'+id+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note})})).json();
if(r.error){rsmsg.textContent='✗ '+r.error;rsgo.disabled=false}else{sig=null}}catch(e){rsmsg.textContent='✗ '+e;rsgo.disabled=false}}}}
const nb=document.getElementById('nb'),dp=document.getElementById('dp'),drepo=document.getElementById('drepo'),disslist=document.getElementById('disslist'),dmsg=document.getElementById('dmsg');
nb.onclick=()=>{dp.hidden=!dp.hidden;if(!dp.hidden)loadRepos()};
async function loadRepos(){try{const d=await(await fetch('/api/repos')).json();drepo.innerHTML='<option value="">— pick a repo —</option>'+(d.repos||[]).map(r=>'<option value="'+esc(r.path)+'">'+esc(r.name)+'</option>').join('')}catch(e){}}
document.getElementById('dload').onclick=async()=>{if(!drepo.value){dmsg.textContent='pick a repo';return}dmsg.textContent='loading issues…';try{const d=await(await fetch('/api/issues?repo='+encodeURIComponent(drepo.value))).json();if(d.error){dmsg.textContent='✗ '+d.error;return}if(!(d.issues||[]).length){dmsg.textContent='no open issues';disslist.hidden=true;return}disslist.innerHTML='<option value="">— pick an issue ('+d.issues.length+') —</option>'+d.issues.map(i=>'<option value="'+esc(i.url)+'">#'+i.number+' '+esc(i.title)+'</option>').join('');disslist.hidden=false;dmsg.textContent=''}catch(e){dmsg.textContent='✗ '+e}};
disslist.onchange=()=>{if(disslist.value)document.getElementById('diss').value=disslist.value};
document.getElementById('dgo').onclick=async()=>{const body={repo:drepo.value,issue:document.getElementById('diss').value.trim(),brief:document.getElementById('dbrief').value.trim()};if(!body.repo){dmsg.textContent='pick a repo';return}if(!body.issue&&!body.brief){dmsg.textContent='pick an issue or write a brief';return}dmsg.textContent='dispatching…';try{const d=await(await fetch('/api/dispatch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();if(d.error){dmsg.textContent='✗ '+d.error;return}dmsg.textContent='✓ queued '+d.id;document.getElementById('diss').value='';document.getElementById('dbrief').value='';disslist.hidden=true;sig=null}catch(e){dmsg.textContent='✗ '+e}};
let showArchived=false,es=null;
function connect(){if(es)es.close();const q=showArchived?'?all=1':'';es=new EventSource('/events'+q);es.onopen=()=>{document.getElementById('conn').textContent='live'};es.onmessage=e=>render(JSON.parse(e.data));es.onerror=()=>{document.getElementById('conn').textContent='reconnecting…'};
fetch('/api/tasks'+q).then(r=>r.json()).then(render).catch(()=>{})}
document.getElementById('ab').onclick=()=>{showArchived=!showArchived;sig=null;connect()};
// --- session-mined rule suggestions (issue #7) — captain approves/rejects, never auto-applied ---
async function loadSuggestions(){try{const d=await(await fetch('/api/suggestions')).json();const list=d.suggestions||[];const sec=document.getElementById('sug'),ul=document.getElementById('suglist');
if(!list.length){sec.hidden=true;return}sec.hidden=false;
const byRepo={};for(const s of list){(byRepo[s.repo]=byRepo[s.repo]||[]).push(s)}
let h='';for(const repo of Object.keys(byRepo).sort()){h+='<div class=meta style="margin-top:8px"><b>'+esc(repo)+'</b></div>';
for(const s of byRepo[repo]){h+='<div class=card style="padding:10px;margin-top:6px"><div>'+esc(s.text)+'</div>'+
(s.evidence?'<div class=last style="opacity:.7">"'+esc(s.evidence)+'"</div>':'')+
'<div class=meta>'+esc(s.source||'')+'</div>'+
'<div class=retryrow><button class=sug-ok data-id="'+s.id+'">Approve</button><button class=sug-no data-id="'+s.id+'">Reject</button></div></div>'}}
ul.innerHTML=h;
ul.querySelectorAll('.sug-ok').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const r=await(await fetch('/api/suggestion/'+b.dataset.id+'/approve',{method:'POST'})).json();if(r.error){alert(r.error);b.disabled=false}else{loadSuggestions()}}catch(e){alert(e);b.disabled=false}});
ul.querySelectorAll('.sug-no').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const r=await(await fetch('/api/suggestion/'+b.dataset.id+'/reject',{method:'POST'})).json();if(r.error){alert(r.error);b.disabled=false}else{loadSuggestions()}}catch(e){alert(e);b.disabled=false}});
}catch(e){}}
document.getElementById('sugtoggle').onclick=()=>{const l=document.getElementById('suglist');l.hidden=!l.hidden;document.getElementById('sugtoggle').textContent=l.hidden?'Show':'Hide'};
loadSuggestions();setInterval(loadSuggestions,15000);
document.getElementById('rb').onclick=async()=>{if(!confirm('Restart coxd now? Refuses if anything is active.'))return;
try{const r=await(await fetch('/api/restart',{method:'POST'})).json();if(r.error){alert(r.error)}else{alert('Restarting — the board will go blank for a few seconds.')}}catch(e){}};
connect();
</script></body></html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8791, log_level="warning")  # noqa: S104
