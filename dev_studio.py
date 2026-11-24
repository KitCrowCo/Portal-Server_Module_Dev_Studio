# /modules/dev_studio/router.py
"""
Note: the tab id is not the path, active is tab id not the path.
"""

# import httpx
# from contextlib import contextmanager
import os, re, time, subprocess, shutil, pathlib, uuid, json, sqlite3
from datetime import datetime
from typing import Optional
from pathlib import Path
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse

from modules.dev_studio.visual_editor import router as visual_router, visual_editor_root

DATA_DIR = Path("./data/dev_studio")
CODE_EDITOR_ROOT = pathlib.Path(os.getenv("CODE_EDITOR_ROOT", os.getcwd())).resolve()
MAX_UPLOAD_BYTES = int(os.getenv("UPLOAD_LIMIT", 2048 * 1024 * 1024))
MODULE_META = {"label":"Development Studio", "icon":"", "description":"IDE Suite for Portal Server development", "persistence":"single", "singelton": True}

ENV = {"db":None, "auth":None, "templates":None, "theme":{}, "tools":{}, "get_state":None, "set_state":None, "clear_state":None, "send_push":None, "broadcast_push":None, "push_fragment":None}
IM = None
TM = None
FM = None
CM = None
CM_SHELL = None
UI = None
md_plus_transpiler = None
tab_bar_from_state = None
IMResponse = None
push_to_client = None
_P = "/module/dev_studio"
_SHELL_HISTORY: list = []  # [(cmd, output)] - process-lifetime only, intentionally not persisted

router = APIRouter()
router.include_router(visual_router, prefix="/visual", tags=["visual-editor"])

# --- State ---

async def dev_state(request: Request, state: dict = None) -> dict:
    if state is not None:
        await ENV["set_state"](request, state, scope="user", namespace="dev_studio")
        return state
    state = await ENV["get_state"](request, scope="user", namespace="dev_studio") or {}
    state.setdefault("tabs", {})
    state.setdefault("active", None)
    state.setdefault("launcher", {"mode":"files", "cwd":""})
    return state

def _git_cfg(): return {}

#######################################################################
# Process taken over by file manager, change backup location to data directory (shadow, or add path into name)

# --- Path / File Helpers ---

def resolve_path(rel_path: str) -> pathlib.Path:
    if rel_path in (None, "", "."): return CODE_EDITOR_ROOT
    p = (CODE_EDITOR_ROOT / rel_path).resolve()
    try: p.relative_to(CODE_EDITOR_ROOT)
    except Exception as e: raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    return p

def get_file_meta_info(p: pathlib.Path) -> dict: return {"size": UI.human_size(p), "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%H:%M:%S")}
def safe_read_text(p: pathlib.Path) -> str:
    with p.open("r", encoding="utf-8", errors="surrogateescape") as f: return f.read()

BACKUPS = DATA_DIR / "backups"   # a copy of a file before Dev Studio writes over it: <its path under the code root>.<time>.bak, never beside the code (it would be synced and listed with it)

def backup_before_write(p: pathlib.Path):
    if not p.exists(): return
    to = BACKUPS / f"""{p.relative_to(CODE_EDITOR_ROOT).as_posix()}.{datetime.utcnow().strftime("%Y%m%dT%H%M%S")}.bak"""
    to.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, to)

def ensure_parent_exists(p: pathlib.Path): p.parent.mkdir(parents=True, exist_ok=True)

#######################################################################

# --- Style ---

studio_style = """
.tab.active { background:var(--surface_bright,var(--bg)) !important; border-bottom:2px solid var(--accent) !important; }
#studio-content-wrap { height:100%; overflow:hidden; }
#editor-stack { height:100%; overflow:hidden; }

.tree-node summary { display:flex; align-items:center; gap:0.5rem; cursor:pointer; list-style:none; }
.tree-leaf { display:flex; align-items:center; gap:0.5rem; cursor:pointer; padding:0.2rem; }
.tree-menu { margin-left:auto; opacity:0.4; cursor:pointer; }
.tree-node[open] > summary .tree-icon { transform:rotate(90deg); }
.tree-icon { display:inline-block; transition:transform 0.2s ease; }
.tree-leaf:hover, .tree-node summary:hover { background:var(--accent_dim); color:var(--accent); }
"""

# --- IM Intent Handlers ---

async def _intent_persist_wip(request, payload, imr):
    """Store in-editor content/scroll/cursor without triggering UI refresh."""
    tab_id = payload.get("id", "")
    editor = await dev_state(request)
    if tab_id in editor.get("tabs", {}):
        tab = editor["tabs"][tab_id]
        tab["wip_content"] = payload.get("content")
        try: tab["wip_scroll"] = float(payload.get("scroll", 0))
        except: tab["wip_scroll"] = None
        try: tab["wip_cursor"] = json.loads(payload.get("cursor", "null"))
        except: tab["wip_cursor"] = None
        await dev_state(request, editor)
    return imr  # no OOB - silent call

#################################################################
# Should be Handled by tab manager (revert maybe)

async def _intent_revert(request, payload, imr):
    tab_id = payload.get("id","")
    editor = await dev_state(request)
    if tab_id in editor.get("tabs", {}):
        tab = editor["tabs"][tab_id]
        tab.pop("wip_content", None); tab.pop("wip_scroll", None); tab.pop("wip_cursor", None)
        tab["dirty"] = False
        await dev_state(request, editor)
        imr.oob((await _render_file_page(request, tab["path"], editor)).body.decode(), "editor-stack")
        imr.oob(await tab_bar_from_state(editor, "studio-tab-bar-wrap", "dev_studio"), "studio-tab-bar-wrap", swap="outerHTML")
    return imr

async def _intent_reorder(request, payload, imr):
    editor = await dev_state(request)
    src, dst = payload.get("from",""), payload.get("to","")
    keys = list(editor["tabs"].keys())
    if src in keys and dst in keys:
        si, di = keys.index(src), keys.index(dst)
        keys.insert(di, keys.pop(si))
        editor["tabs"] = {k: editor["tabs"][k] for k in keys}
        for i, k in enumerate(keys): editor["tabs"][k]["order"] = i
    await dev_state(request, editor)
    imr.oob(await tab_bar_from_state(editor, "studio-tab-bar-wrap", "dev_studio"), "studio-tab-bar-wrap", swap="outerHTML")
    return imr
#################################################################

# --- File Rendering (level 2 shells) ---

_CM_MODES = {"py":"python", "js":"javascript", "html":"htmlmixed", "css":"css", "md":"markdown", "yml":"yaml"}
_LAUNCHER_HTML = '<div style="height:100%; display:flex; align-items:center; justify-content:center; opacity:0.4; font-size:1.2rem;">Open a file from the tree</div>'

async def _render_active_tab_html(request, state: dict):
    """Called by TM to produce content area HTML on tab changes."""
    state.setdefault("tabs", {})
    state.setdefault("active", None)
    active = state.get("active")
    if not active or active not in state["tabs"]: return state, _LAUNCHER_HTML
    path = state["tabs"][active].get("path", "")
    if not path: return state, _LAUNCHER_HTML
    html = await _render_file_page(request, path, state)
    return state, html.body.decode()

async def _render_file_page(request: Request, path: str, state: dict = None) -> HTMLResponse:
    """Render file content fragment for #editor-stack. Plain HTMLResponse, no template shell."""
    if not path or path.startswith("untitled") or path == "/launcher": return HTMLResponse(_LAUNCHER_HTML)   # "/launcher" is the tab the TabManager makes when the last tab closes - here the start view, not a file
    if path.startswith("design://"): return await visual_editor_root(path.replace("design://",""))
    if path.startswith("console://"): return HTMLResponse('<div style="padding:2rem;color:#ff5f5f;">Console not yet integrated</div>')
    if not state: state = await dev_state(request)

    # find the tab object for wip state - prefer active tab, fall back to path search
    active = state.get("active")
    tab = state["tabs"].get(active, {}) if active else {}
    if tab.get("path") != path: tab = next((t for t in state["tabs"].values() if t.get("path") == path), {})
    tab_id = tab.get("id", "")

    p = resolve_path(path)
    ext = p.suffix.lower()

    if ext in (".png",".jpg",".jpeg",".gif",".svg",".webp"): return HTMLResponse(f"""<div style="height:100%;display:flex;align-items:center;justify-content:center;padding:1rem;"><img src="{_P}/raw?path={path}" style="max-width:100%;max-height:100%;object-fit:contain;"></div>""")

    if ext == ".csv":
        try:
            rows = [r.split(",") for r in safe_read_text(p).splitlines() if r.strip()]
            body = UI.table(rows[0], rows[1:]) if len(rows) > 1 else "<p>Empty CSV</p>"
        except Exception as e: body = f'<p style="color:#ff5f5f;">Error: {UI.escape(str(e))}</p>'
        return HTMLResponse(f'<div style="overflow:auto;padding:1rem;height:100%;">{body}</div>')

    try: content = tab.get("wip_content") or safe_read_text(p)
    except Exception as e: return HTMLResponse(f'<div style="padding:2rem;color:#ff5f5f;">Error reading: {UI.escape(str(e))}</div>')

    cm_mode  = _CM_MODES.get(ext[1:], "text/plain")
    safe_id  = path.replace("/","_").replace(".","_").replace("-","_")
    dirty_dot = '<span style="color:#ffaa44;margin-left:0.3rem;">&bull;</span>' if tab.get("dirty") else ""
    has_wip  = bool(tab.get("wip_content"))
    scroll_val = UI.escape(str(tab.get("wip_scroll","") or ""))
    cursor_val = UI.escape(json.dumps(tab.get("wip_cursor")) if tab.get("wip_cursor") else "")

    revert_btn = (f"""<button class="ui-btn" style="color:#ffaa44;padding:0.1rem 0.4rem;font-size:0.75rem;" hx-post="/im/in" hx-swap="none" hx-vals='{json.dumps({"type":"dev_studio_revert","id":tab_id})}'>&#x21A9;</button>""") if has_wip else ""
    save_btn = f"""<button class="ui-btn" style="padding:0.1rem 0.4rem;font-size:0.75rem;" hx-post="{_P}/save" hx-include="#form-{safe_id}" hx-swap="none" hx-on::before-request="var ta=document.getElementById('editor-{safe_id}');if(ta&&ta.cm)ta.cm.save()">Save</button>"""

    # CM init: size to parent, persist WIP on change, restore scroll/cursor on load
    file_script = f"""(function(){{
var area=document.getElementById('editor-{safe_id}');
if(!area||area.cm)return;
area.cm=CodeMirror.fromTextArea(area,{{lineNumbers:true,theme:'default',mode:'{cm_mode}',indentUnit:4}});
function sz(){{var p=area.closest('#editor-stack');if(p)area.cm.setSize('100%',(p.clientHeight-32)+'px');}}
requestAnimationFrame(sz);window.addEventListener('resize',sz);
area.cm.on('change',function(){{
    if(area._ct)clearTimeout(area._ct);
    area._ct=setTimeout(function(){{area.cm.save();
        htmx.ajax('POST','/im/in',{{values:{{type:'dev_studio_persist_wip',id:'{tab_id}',content:area.value,scroll:String(area.cm.getScrollInfo().top),cursor:JSON.stringify(area.cm.getCursor())}},swap:'none',target:document.body}});
    }},600);
}});
requestAnimationFrame(function(){{try{{
    var ws=area.dataset.wipScroll,wc=area.dataset.wipCursor;
    if(ws)area.cm.scrollTo(0,Number(ws));
    if(wc){{var c=JSON.parse(wc);if(c&&c.line!=null)area.cm.setCursor(c);}}
    area.cm.refresh();
}}catch(e){{}}}});
}})();"""

    return HTMLResponse(f"""<div style="height:100%;display:flex;flex-direction:column;">
    <div style="display:flex;align-items:center;gap:0.4rem;padding:0.15rem 0.5rem;border-bottom:var(--border-thick) solid var(--border);flex-shrink:0;background:var(--bg_panel);">
    <span style="font-size:0.72rem;color:var(--text_muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{UI.escape(path)}">{UI.escape(path)}{dirty_dot}</span>{save_btn}{revert_btn}</div>
    <form id="form-{safe_id}" style="flex:1;min-height:0;overflow:hidden;">
    <textarea id="editor-{safe_id}" name="content" data-mode="{cm_mode}" data-path="{UI.escape(path)}" data-tab-id="{UI.escape(tab_id)}" data-wip-scroll="{scroll_val}" data-wip-cursor="{cursor_val}" style="display:none;">{UI.escape(content)}</textarea><input type="hidden" name="path" value="{UI.escape(path)}"><input type="hidden" name="tab_id" value="{UI.escape(tab_id)}"></form><script>{file_script}</script></div>""")

# -- Module Integration --

def init_module(environment: dict):
    global ENV, IM, TM, UI, md_plus_transpiler, tab_bar_from_state, IMResponse, push_to_client, FM, CM, CM_SHELL
    ENV.update(environment)
    UI = ENV.get("templates").env.globals.get("UI")
    md_plus_transpiler = ENV["tools"]["built_ins"].md_plus_transpiler
    tab_bar_from_state = ENV["tools"]["built_ins"].tab_bar_from_state
    IMResponse = ENV["IMResponse"]
    push_to_client = ENV["push_to_client"]
    IM = ENV["InterfaceManager"](nesting_level = 1, db_path = f"""{ENV["data_dir"]}/im_registry.db""")
    TM = ENV["tools"]["built_ins"].TabManager(namespace = "dev_studio", tab_bar_id = "studio-tab-bar-wrap", content_id = "editor-stack", render_content_fn = _render_active_tab_html, intent_prefix = "dev_studio", IM = IM)
    FM = ENV["tools"]["built_ins"].FileManager(CODE_EDITOR_ROOT)
    CM = ENV["tools"]["built_ins"].ChatManager(namespace="dev_studio_dev_logs", base_url=_P, view_style="log", input_enabled=False, show_avatars=False, allow_edit=False, allow_delete=False, allow_copy=True, show_info=False, markdown_mode="standard", branch_id=IM.branch_id, nesting_level=1)
    CM_SHELL = ENV["tools"]["built_ins"].ChatManager(namespace="dev_studio_dev_shell", base_url=_P, view_style="log", input_enabled=True, placeholder="Command\u2026 (Ctrl+Enter to run)", show_avatars=False, allow_edit=False, allow_delete=False, allow_copy=True, show_info=False, markdown_mode="standard", stream_toggle=False, think_toggle=False, stop_enabled=False, show_export=False, branch_id=IM.branch_id, nesting_level=1, intent_name="dev_studio_shell_run", IM=IM, on_submit=_intent_shell_run)
    IM.scripts["dev_studio_persist_wip"] = [_intent_persist_wip]
    IM.scripts["dev_studio_revert"] = [_intent_revert]
    IM.scripts["dev_studio_reorder"] = [_intent_reorder]
    print("Development Studio: environment synchronized.")

# --- Main Page ---

@router.get("/", response_class=HTMLResponse)
async def studio_main(request: Request):
    editor = await dev_state(request)
    editor, file_content_html = await _render_active_tab_html(request, editor)
    tab_bar_html = await tab_bar_from_state(editor, "studio-tab-bar-wrap", "dev_studio")

    left_bar = f"""<div style="display:flex;flex-direction:column;height:100%;">
        <div style="display:flex;gap:0.2rem;padding:0.3rem;flex-wrap:wrap;border-bottom:var(--border-thick) solid var(--border);">
            {UI.icon_button("&#x1F4C1;", hint="Files", htmx={"post":f"{_P}/launcher_mode","vals":'{"mode":"files"}', "target":"#launcher-panel","swap":"innerHTML"})}
            {UI.icon_button("&#x1F9E9;", hint="Modules", htmx={"post":f"{_P}/launcher_mode","vals":'{"mode":"modules"}', "target":"#launcher-panel","swap":"innerHTML"})}
            {UI.icon_button("&#x7B;&#x7D;", hint="Dictionary", htmx={"post":f"{_P}/launcher_mode","vals":'{"mode":"dict"}', "target":"#launcher-panel","swap":"innerHTML"})}
        </div>
        <div style="display:flex;gap:0.2rem;padding:0.3rem;border-bottom:var(--border-thick) solid var(--border);">
            {UI.icon_button("&#x2795;", hint="New file / folder / upload", htmx={"get":f"{_P}/new_modal","target":"#dev-new-modal","swap":"innerHTML"})}
        </div>
        <div id="launcher-panel" {UI.htmx_html({"get":f"{_P}/launcher_tree","trigger":"load, launcherRefresh from:body","target":"this"})} style="flex:1;overflow:auto;font-size:0.82rem;"></div>
    </div>"""
    #right_bar = f"""<div id="right-sidebar-outer" hx-get="{_P}/right_sidebar/state" hx-trigger="load" hx-target="this"></div>"""

    # Need to add in the save all - title and other status parts *****************************************************************
    top_bar_content = f'<div id="studio-tab-bar-wrap" style="height:100%;">{tab_bar_html}</div>'
    right_bar = f"""<div id="right-sidebar-mount" hx-get="{_P}/right_sidebar/logs" hx-trigger="load" hx-target="this" style="height:100%;overflow:hidden;display:flex;flex-direction:column;"></div>"""
    return ENV["templates"].TemplateResponse(name = "base.html", request = request, context = {
        "request": request, "user": request.state.user, "nesting_level": 1, "code_mirror": True,
        "toolbars": {"top": UI.toolbar(side="top", content=top_bar_content, size="3rem", overlay=False, nesting_level=1, start_open=True, locked=True),
                     "left": UI.toolbar(side="left", content=left_bar, size="16rem", overlay=False, nesting_level=1, start_open=False, locked=False, resizable=True),
                     "right": UI.toolbar(side="right", content=right_bar, size="16rem", overlay=False, nesting_level=1, start_open=False, locked=False, resizable=True)},
        "content": f'<div id="studio-content-wrap"><div id="editor-stack">{file_content_html}</div></div><div id="dev-new-modal"></div>',
        "extra_css": studio_style + CM.CSS, "extra_script": ENV["tools"]["built_ins"].PORTAL_EDITOR_JS})

# --- File Tree / Launcher ---

@router.get("/launcher")
async def launcher(request: Request): return HTMLResponse(_LAUNCHER_HTML)

@router.get("/launcher_tree")
async def launcher_tree(request: Request, editor=None):
    if not editor: editor = await dev_state(request)
    mode = editor["launcher"]["mode"]
    root = resolve_path(editor["launcher"]["cwd"] or "")
    active_path = editor.get("tabs", {}).get(editor.get("active",""), {}).get("path", "")
    if mode == "files": return HTMLResponse(UI.tree(items=root, mode="file", active=active_path, options={"post":"/im/in","target":"body","swap":"none","extra_vals":{"type":"dev_studio_open_tab", "label":""}}, context_menu_url=f"{_P}/ctx_menu"))
    if mode == "dict": return HTMLResponse(UI.tree(items=editor.get("dict_view", {}), mode="dict", options={"target":"#editor-stack","swap":"innerHTML"}))
    if mode == "modules": return HTMLResponse("<div style='padding:1rem;'>Module templates - coming soon.</div>")
    return HTMLResponse("")

@router.post("/launcher_mode")
async def set_launcher_mode(request: Request, mode: str = Form(...)):
    editor = await dev_state(request)
    editor["launcher"]["mode"] = mode
    await dev_state(request, editor)
    return await launcher_tree(request, editor)

@router.get("/cleanup", response_class=HTMLResponse)
async def cleanup_page(request: Request): return HTMLResponse(_cleanup_html())

@router.post("/cleanup", response_class=HTMLResponse)
async def cleanup_kept(request: Request):
    """The ticked backup copies and trashed files deleted, the ticked sync backup branches removed (admin only)."""
    if getattr(request.state.user, "role", "") != "admin": return HTMLResponse(_cleanup_html("only an admin removes backups", False))
    form, said = await request.form(), []
    for rel in form.getlist("files"):
        p = _kept_path(str(rel))
        if p.exists(): shutil.rmtree(p) if p.is_dir() else p.unlink()
        said.append(f"{rel}: removed")
    said += _gs()["drop_kept"]([{"folder": x.split("|", 1)[0], "branch": x.split("|", 1)[1]} for x in form.getlist("branches")]) if form.getlist("branches") else []
    return HTMLResponse(_cleanup_html(f"""{len(said)} removed. """ + "; ".join(said[-5:]) if said else "Nothing was ticked."))

@router.post("/scaffold_module")
async def scaffold_module(name: str = Form(...)):
    if not name.isalnum(): raise HTTPException(status_code=400, detail="Invalid module name")
    base = CODE_EDITOR_ROOT / "modules" / name
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / "router.py").write_text(f"from fastapi import APIRouter\nrouter = APIRouter()\n\n@router.get('/')\nasync def root():\n    return {{'module': '{name}'}}", encoding="utf-8")
        (base / "templates").mkdir(exist_ok=True)
        (base / "templates" / f"{name}.html").write_text("<div>Module UI</div>", encoding="utf-8")
        return HTMLResponse(f"&#x2705; Module '{name}' scaffolded.", headers={"HX-Trigger":"launcherRefresh"})
    except Exception as e: return HTMLResponse(f"&#x26A0; Scaffolding failed: {UI.escape(str(e))}", status_code=500)

@router.post("/restore_backup")
async def restore_backup(request: Request):
    form = await request.form()
    file, backup_file = form.get("file"), form.get("backup_file")
    if not file or not backup_file: raise HTTPException(status_code=400)
    backup = resolve_path(backup_file)
    if not backup.exists(): raise HTTPException(status_code=404)
    shutil.copy2(backup, resolve_path(file))
    return HTMLResponse("Restored")

######################################################################
# Should be in filemanager

# --- File Operations ---

@router.post("/save")
async def save_file(request: Request):
    form    = await request.form()
    path    = form.get("path", "").strip()
    content = form.get("content")
    if not path: return HTMLResponse("&#x26A0; No path", status_code=400)
    if content is None: return HTMLResponse("&#x26A0; No content", status_code=400)
    p = resolve_path(path)
    if p.exists() and p.is_dir(): return HTMLResponse("&#x26A0; Is a directory", status_code=400)
    editor = await dev_state(request)
    try:
        ensure_parent_exists(p)
        backup_before_write(p)
        with p.open("w", encoding="utf-8", errors="surrogateescape") as f: f.write(content)
        for tid, tab in editor.get("tabs", {}).items():
            if tab.get("path") == path:
                tab["dirty"] = False
                tab.pop("wip_content", None); tab.pop("wip_scroll", None); tab.pop("wip_cursor", None)
        await dev_state(request, editor)
        meta = get_file_meta_info(p)
        imr = await TM._push(request, editor, IMResponse())
        restart = _gs()["code_saved"](str(p.relative_to(pathlib.Path.cwd().resolve())) if p.is_relative_to(pathlib.Path.cwd().resolve()) else path)   # a dev instance (sync.json instance.restart_on_code_change) restarts when a code file changes
        resp = imr.build(f"""&#x2705; {meta['modified']}{" - restarting" if restart else ""}""")
        return resp
    except Exception as e: return HTMLResponse(f"&#x26A0; {e}", status_code=500)

@router.get("/raw")
async def get_raw_file(path: str): return FileResponse(resolve_path(path))

@router.get("/new_modal")
async def new_modal_route(request: Request):
    editor = await dev_state(request)
    return HTMLResponse(ENV["tools"]["built_ins"].new_item_modal_html("dev-new", f"{_P}/new", FM.folder_picker_html(editor["launcher"]["cwd"] or "")))

@router.post("/new")
async def new_file_or_folder(request: Request, parent: str = Form(""), kind: str = Form("file"), name: str = Form(""), upload: list[UploadFile] = File(default=[]), rel_paths: str = Form("")):
    if kind in ("upload", "upload_folder"):
        rels = json.loads(rel_paths or "[]")
        for i, f in enumerate(upload):
            if not f.filename: continue
            rel = FM.safe_rel(rels[i]) if i < len(rels) else FM.safe_rel(f.filename)
            dest = FM.resolve(parent) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            size, content = 0, b""
            while chunk := await f.read(65536):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES: raise HTTPException(status_code=413)
                content += chunk
            dest.write_bytes(content)
        return HTMLResponse("", headers={"HX-Trigger":"launcherRefresh"})
    if kind == "folder":
        if name.strip(): FM.safe_join(parent, name).mkdir(parents=True, exist_ok=True)
        return HTMLResponse("", headers={"HX-Trigger":"launcherRefresh"})
    p = FM.safe_join(parent, name or f"untitled-{uuid.uuid4().hex[:6]}.txt")
    if p.exists(): raise HTTPException(status_code=400, detail="File exists")
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text("", encoding="utf-8")
    rel_path = str(p.relative_to(CODE_EDITOR_ROOT)).replace("\\", "/")
    tab_id = f"file-{uuid.uuid4().hex[:6]}"
    imr = await TM._open(request, {"id": tab_id, "path": rel_path, "label": p.name}, IMResponse())
    resp = imr.build(); resp.headers["HX-Trigger"] = "launcherRefresh"
    return resp

@router.post("/delete")
async def delete_path(request: Request):
    form = await request.form()
    path = form.get("path")
    if not path: raise HTTPException(status_code=400, detail="Missing path")
    p = resolve_path(path)
    if not p.exists(): raise HTTPException(status_code=404)
    trash = CODE_EDITOR_ROOT / ".trash"; trash.mkdir(exist_ok=True)
    shutil.move(str(p), str(trash / f"{p.name}.{uuid.uuid4().hex[:6]}"))
    return HTMLResponse("Moved to .trash", headers={"HX-Trigger":"launcherRefresh"})

@router.post("/rename")
async def rename_path(request: Request):
    form = await request.form()
    path, new_name = form.get("path"), form.get("new_name")
    p = resolve_path(path)
    if not p.exists(): raise HTTPException(status_code=404)
    target = p.with_name(new_name)
    if target.exists(): raise HTTPException(status_code=400, detail="Target exists")
    p.rename(target)
    return HTMLResponse("Renamed", headers={"HX-Trigger":"launcherRefresh"})

@router.get("/download")
async def download_file(path: str):
    p = resolve_path(path)
    if not p.exists() or p.is_dir(): raise HTTPException(status_code=404)
    return FileResponse(p, filename=p.name)

@router.get("/move_modal", response_class=HTMLResponse)
async def move_modal_route(path: str):
    return HTMLResponse(ENV["tools"]["built_ins"].move_modal_html("dev-move", f"{_P}/move", FM.folder_picker_html(), path))

@router.post("/move", response_class=HTMLResponse)
async def move_item(path: str = Form(...), parent: str = Form("")):
    FM.move(path, parent)
    return HTMLResponse("Moved", headers={"HX-Trigger":"launcherRefresh"})

@router.get("/refresh")
async def global_refresh():
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({"launcherRefresh":True})})

@router.get("/ctx_menu", response_class=HTMLResponse)
async def ctx_menu(path: str):
    return HTMLResponse(f"""<div style="padding:.3rem .5rem;display:flex;flex-direction:column;gap:.25rem;font-size:.75rem">
        <button class="btn-icon" style="text-align:left" hx-get="{_P}/move_modal?path={UI.escape(path)}" hx-target="#dev-new-modal" hx-swap="innerHTML">&#x21C4; Move</button>
        <button class="btn-icon" style="text-align:left;color:#ff5f5f" hx-post="{_P}/delete" hx-vals='{{"path":"{UI.escape(path)}"}}' hx-swap="none" hx-confirm="Delete {UI.escape(path)}?">&#x2715; Delete</button>
    </div>""")

# --- Right Sidebar ---

@router.get("/right_sidebar/{mode}")
async def get_right_sidebar(mode: str, request: Request):
    MODES = [("&#x2699;","state"),("&#x1F4CB;","logs"),("&#x2328;","prompt"),("&#x2387;","git"),("&#x1F3A8;","palette"),("&#x2753;","help")]
    valid = {m for _,m in MODES}
    if mode == "debug": mode = "state"
    if mode not in valid: mode = "logs"
    if mode == "state":
        content = f'<div style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;"><div id="debug-state-inner" hx-get="{_P}/debug_state" hx-trigger="load" hx-target="this" hx-swap="innerHTML" style="flex:1;min-height:0;overflow:hidden;"><div style="padding:0.4rem;color:var(--text_muted);font-size:0.75rem;">Loading...</div></div></div>'
    elif mode == "logs":
        content = f'<div style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;"><div id="debug-logs-inner" hx-get="{_P}/debug_logs" hx-trigger="load" hx-target="this" hx-swap="innerHTML" style="flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column;"><div style="padding:0.4rem;color:var(--text_muted);font-size:0.75rem;">Loading...</div></div></div>'
    elif mode == "prompt":
        content = f'<div style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;">{_shell_panel_html()}</div>'
    elif mode == "git":
        content = f"""<div style="flex:1;min-height:0;overflow-y:auto;padding:.5rem;display:flex;flex-direction:column;gap:.4rem"><button class="ui-btn" onclick="htmx.ajax('POST','/im/in',{{values:{{type:'_portal_open_tab',path:'{_P}/sync',label:'Sync',id:'dev-sync-tab',lvl:0}},swap:'none'}})">&#x21C5; Open Sync</button><div class="status-label">Check, pull and push this portal's code folders against its git servers, by the rules in {UI.escape(_gs()["config_path"])}.</div></div>"""
    elif mode == "palette":
        import inspect as _inspect
        items = "".join(f'<div style="padding:0.3rem 0.5rem;font-size:0.8rem;cursor:grab;border-bottom:1px solid var(--border);">{f}</div>' for f in [n for n,_ in _inspect.getmembers(UI, predicate=_inspect.isfunction) if not n.startswith("_")])
        content = f'<div style="flex:1;min-height:0;overflow-y:auto;">{items}</div>'
    elif mode == "help":
        help_path = CODE_EDITOR_ROOT / "tools" / "dev_studio" / "HELP.md"
        content = (f'<div style="flex:1;min-height:0;overflow-y:auto;padding:0.8rem;font-size:0.8rem;">{md_plus_transpiler(help_path.read_text(encoding="utf-8",errors="replace"))}</div>' if help_path.exists()
                    else '<div style="flex:1;overflow-y:auto;padding:1rem;font-size:0.8rem;"><p style="color:var(--text_muted);">Place <code>tools/dev_studio/HELP.md</code> for local docs.</p></div>')
    else:
        content = '<div style="padding:1rem;color:var(--text_muted);">Unknown mode</div>'

    tabs = "".join(f'<div class="sidebar-tab {"active" if mode==m else ""}" hx-get="{_P}/right_sidebar/{m}" hx-target="#right-sidebar-mount" title="{m}" style="cursor:pointer;padding:0.45rem 0.6rem;font-size:1rem;{"color:var(--accent);" if mode==m else "color:var(--text_muted);"}">{icon}</div>' for icon,m in MODES)
    return HTMLResponse(f'<div style="display:flex;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--bg_panel);">{tabs}</div><div id="right-sidebar-inner" style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;">{content}</div>')

# --- Debug ---

@router.get("/debug_state")
async def debug_state(request: Request):
    editor = await dev_state(request)
    active = editor.get("active","none")
    return HTMLResponse(f"""<div style="flex:1;min-height:0;display:flex;flex-direction:column;font-family:monospace;font-size:0.72rem;overflow:hidden;"><div style="padding:0.25rem 0.5rem;flex-shrink:0;display:flex;justify-content:space-between;align-items:center;background:var(--surface);border-bottom:var(--border-thick) solid var(--border);"><span style="color:var(--accent);">State &mdash; <b>{UI.escape(active)}</b></span><span style="display:flex;gap:0.3rem;"><button class="ui-btn" style="font-size:0.65rem;padding:0.1rem 0.3rem;" hx-get="{_P}/debug_state" hx-target="#debug-state-inner" hx-swap="innerHTML">&#x21BA;</button><button class="ui-btn" style="font-size:0.65rem;padding:0.1rem 0.3rem;" hx-post="/im/in" hx-vals='{json.dumps({"type":"dev_studio_revert","id":active})}' hx-swap="none">&#x21A9; Revert WIP</button></span></div><pre style="flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;white-space:pre-wrap;word-break:break-all;margin:0;padding:0.4rem;">{UI.escape(json.dumps(editor, indent=2))}</pre></div>""")

@router.get("/debug_logs")
async def debug_logs():
    cid, server_name = os.environ.get("HOSTNAME","portal_server"), os.environ.get("SERVER_NAME","portal")
    logs = log_source = ""
    for lpath in ["/tmp/server.log","/proc/1/fd/1"]:
        if logs: break
        try:
            if os.path.exists(lpath):
                r = subprocess.run(["tail","-n","300",lpath], capture_output=True, text=True, timeout=4)
                if r.stdout.strip(): logs, log_source = r.stdout, lpath
        except Exception: pass
    if not logs:
        logs = f"Log not yet available.\nServer: {server_name} ({cid})\n\nuvicorn ... 2>&1 | tee /tmp/server.log"
        log_source = "unavailable"
    msg = {"id": "logtail", "role": "system", "content": f"```\n{logs}\n```", "user_name": "", "timestamp": ""}
    rendered = CM.render_messages([msg], viewer_name="")
    return HTMLResponse(f"""<div style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;">
        <div style="padding:0.2rem 0.2rem;flex-shrink:0;display:flex;justify-content:space-between;align-items:center;background:var(--surface);border-bottom:var(--border-thick) solid var(--border);">
            <span style="color:var(--accent);font-size:.7rem;">Logs &mdash; <span style="color:var(--text_muted);">{UI.escape(server_name)}/{UI.escape(cid)}</span> <span style="font-size:0.6rem;opacity:0.6;">[{UI.escape(log_source)}]</span></span>
            <button class="ui-btn" style="font-size:0.6rem;padding:0.1rem 0.3rem;" hx-get="{_P}/debug_logs" hx-target="#debug-logs-inner" hx-swap="innerHTML">&#x21BA;</button>
        </div>
        <div id="dev-logs-msgs" class="cm-msgs cm-s-log" data-pinned="true" style="flex:1;min-height:0;font-family:var(--font-mono);font-size:.7rem;">{rendered}</div>
    </div>
    <script>(function(){{var e=document.getElementById("dev-logs-msgs");if(e)e.scrollTop=e.scrollHeight;}})();</script>""")

@router.get("/debug_panel")
async def debug_panel(request: Request): return await debug_state(request)

# --- Shell ---

@router.post("/shell_exec")
async def shell_exec(cmd: str = Form(...)):
    if any(b in cmd for b in ["rm ","shutdown","reboot","mkfs","dd ",":(){","chmod 777 /"]): return HTMLResponse("&#x274C; Command blocked", status_code=400)
    try:
        r = subprocess.run(cmd, shell=True, cwd=CODE_EDITOR_ROOT, capture_output=True, text=True, timeout=15)
        return HTMLResponse(f"<pre>{UI.escape(r.stdout + chr(10) + r.stderr)}</pre>")
    except Exception as e: return HTMLResponse(f"<pre>Error: {UI.escape(str(e))}</pre>")

# --- Command Prompt ---
# Reuses ChatManager for a scrollable command+output log, same general mechanism as the logs panel and every other ChatManager consumer in this codebase - not a bespoke shell UI.

async def _intent_shell_run(request, payload, imr):
    cmd = payload.get("content","").strip()
    if not cmd: return imr
    if any(b in cmd for b in ["rm ","shutdown","reboot","mkfs","dd ",":(){","chmod 777 /"]):
        out = "Command blocked"
    else:
        try:
            r = subprocess.run(cmd, shell=True, cwd=CODE_EDITOR_ROOT, capture_output=True, text=True, timeout=15)
            out = (r.stdout + r.stderr).strip() or "(no output)"
        except Exception as e: out = f"Error: {e}"
    _SHELL_HISTORY.append((cmd, out))
    msg = {"id": f"sh{len(_SHELL_HISTORY)}", "role": "assistant", "content": f"$ {cmd}\n```\n{out}\n```", "user_name": "shell", "timestamp": ""}
    imr.raw(CM_SHELL.append_message_html("_dev_shell", msg, viewer_name=""))
    return imr

def _shell_panel_html() -> str:
    msgs = [{"id": f"sh{i}", "role": "assistant", "content": f"$ {c}\n```\n{o}\n```", "user_name": "shell", "timestamp": ""} for i, (c,o) in enumerate(_SHELL_HISTORY)]
    return CM_SHELL.shell("_dev_shell", messages=msgs, viewer_name="")

# --- Database Editor ---

@router.get("/db/open")
async def open_db(path: str):
    p = resolve_path(path)
    if not p.exists(): raise HTTPException(404)
    conn = sqlite3.connect(p)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    tlist = "".join(f"<li><a href='javascript:void(0)' onclick=\"loadDbTable('{path}','{t[0]}')\">{t[0]}</a></li>" for t in tables)
    return HTMLResponse(f'<div style="display:flex;gap:1rem;height:80vh;"><div style="width:20%;border-right:var(--border-thick) solid var(--border);padding:1rem;"><h4>Tables</h4><ul>{tlist}</ul></div><div style="flex:1;display:flex;flex-direction:column;"><div id="db-table-view" style="flex:1;overflow:auto;">Select a table.</div><div style="border-top:var(--border-thick) solid var(--border);padding:0.5rem;"><textarea id="db-query-box" style="width:100%;height:6rem;font-family:monospace;"></textarea><button onclick="runDbQuery(\'{path}\')">Run Query</button></div><div id="db-query-result" style="flex:1;overflow:auto;"></div></div></div>')

@router.get("/db/tables")
async def db_tables(path: str):
    conn = sqlite3.connect(resolve_path(path)); rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(); conn.close()
    return JSONResponse([r[0] for r in rows])

@router.get("/db/table")
async def db_table(path: str, table: str, limit: int = 200):
    conn = sqlite3.connect(resolve_path(path)); conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table} LIMIT ?", (limit,)).fetchall(); conn.close()
    return JSONResponse({"columns": list(rows[0].keys()) if rows else [], "rows": [dict(r) for r in rows]})

@router.post("/db/query")
async def db_query(path: str = Form(...), query: str = Form(...)):
    conn = sqlite3.connect(resolve_path(path)); conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(query)
        if query.strip().lower().startswith("select"):
            rows = cur.fetchall(); result = {"columns": list(rows[0].keys()) if rows else [], "rows": [dict(r) for r in rows]}
        else: conn.commit(); result = {"status":"ok"}
    except Exception as e: result = {"error": str(e)}
    conn.close(); return JSONResponse(result)

@router.get("/db/schema")
async def db_schema(path: str):
    conn = sqlite3.connect(resolve_path(path)); rows = conn.execute("SELECT sql FROM sqlite_master WHERE type='table'").fetchall(); conn.close()
    return JSONResponse([r[0] for r in rows])

# --- Sync: this portal's code folders against its git servers (Git Manager's git_sync service does the work, by data/_tools/git_manager/sync.json) ---

def _gs(): return ENV["services"]["git_sync"]   # Git Manager is a tool, so it is loaded before this module

def _sync_rows(rs: dict, results: dict, push: dict, pull: dict) -> str:
    covers = lambda p: "push and pull" if p in push and p in pull else "push only" if p in push else "pull only" if p in pull else "neither"
    return "".join(f"""<tr><td><input type="checkbox" name="paths" value="{UI.escape(p)}" title="Include this folder - with none ticked, Check / Pull / Push act on every folder that direction covers"></td><td class="mono" style="font-size:.75rem">{UI.escape(p)}</td><td class="status-label">{UI.escape(r)}</td><td class="status-label">{covers(p)}</td><td class="status-label">{UI.escape(results.get(p, ""))}</td></tr>""" for p, r in rs.items())

def _sync_chips(rs: dict) -> str:
    groups = sorted({p.split("/")[0] if p.count("/") < 3 else "/".join(p.split("/")[:3]) for p in rs})
    pick = lambda g: f"""<button type="button" class="chip" onclick="this.closest('form').querySelectorAll('input[name=paths]').forEach(c=>{{if(c.value==='{g}'||c.value.startsWith('{g}/'))c.checked=!c.checked}})">{UI.escape(g)}</button>"""
    return "".join(pick(g) for g in groups) + """<button type="button" class="chip" onclick="this.closest('form').querySelectorAll('input[name=paths]').forEach(c=>c.checked=false)">none</button>"""

def _sync_page(request: Request, target: str = "", out: dict = None, note: str = "", ok: bool = True, typed: str = None) -> str:
    """The Sync view: pick a target, tick folders (none = all), Check / Pull / Push. What each does is the target's rules in the config (shown in the bar)."""
    gs, c = _gs(), _gs()["config"]()
    names = [n for n in gs["targets"](c) if gs["may_use"](c, n, request.state.user)]
    if not names: return f"""<div class="glass" style="padding:.8rem"><b>Sync</b><div class="status-label">No target you may use in {UI.escape(gs["config_path"])}. An admin adds targets (and lists who besides admins may use each).</div>{_sync_config_html(note, ok, typed)}</div>"""
    target = target if target in names else names[0]
    t, inst, last = c["targets"][target], gs["instance"](c), gs["last"]()
    results = (out or {}).get("results") or (last.get("results", {}) if last.get("target") == target else {})
    after = "".join(f"""<div class="status-label">{UI.escape(a)}</div>""" for a in (out or {}).get("after", []))
    rules = f"""push {t.get("push", "force")} &#xB7; pull {t.get("pull", "replace")} &#xB7; history {t.get("history", "keep")} &#xB7; {UI.escape(t["owner"])} / {UI.escape(t.get("branch", "main"))}{" &#xB7; after push: " + UI.escape(", ".join(h["service"] + "." + h["call"] for h in t.get("after_push", []))) if t.get("after_push") else ""}{" &#xB7; after pull: " + UI.escape(", ".join(h["service"] + "." + h["call"] + (" " + str(h.get("args", {}).get("setup", "")) if h.get("args") else "") for h in t.get("after_pull", []))) if t.get("after_pull") else ""}"""
    opts = "".join(f"""<option value="{UI.escape(n)}"{" selected" if n == target else ""}>{UI.escape(n)}</option>""" for n in names)
    rs, push, pull = gs["repos"](c, target), gs["repos"](c, target, "push"), gs["repos"](c, target, "pull")
    listed = "".join(f"""<div class="status-label">{UI.escape(x)}</div>""" for x in gs["explain"](c, target))
    return f"""<div id="dev-sync" class="glass" style="padding:.8rem;display:flex;flex-direction:column;gap:.4rem">
                   <div class="row"><b class="grow">Sync - {UI.escape(inst["name"])} ({UI.escape(inst["role"])})</b><span class="status-label">{"last: " + UI.escape(last.get("op", "") + " " + last.get("target", "") + " " + last.get("at", "").replace("T", " ") + " by " + last.get("user", "")) if last else ""}</span></div>
                   {_sync_mine_html(request, target)}
                   <form class="sync-run" hx-post="{_P}/sync/run" hx-target="#dev-sync" hx-swap="outerHTML" style="display:flex;flex-direction:column;gap:.4rem">
                       <div class="row"><label class="status-label">Target <select name="target" class="module-select" style="width:auto" hx-get="{_P}/sync" hx-target="#dev-sync" hx-swap="outerHTML" hx-trigger="change" hx-include="this">{opts}</select></label><span class="status-label grow">{rules}</span></div>
                       {listed}<div class="row">{_sync_chips(rs)}</div>
                       <div class="scroll-pane" style="max-height:55vh"><table class="data-table" style="width:100%"><tr><th title="Tick the folders to act on; none ticked = all">Only</th><th>Folder</th><th>Repository</th><th title="Which direction this target's list covers for the folder">Covers</th><th>Result</th></tr>{_sync_rows(rs, results, push, pull)}</table></div>
                       <input type="text" name="message" placeholder="Commit message for Push (blank = your name, this portal, date)" class="module-select">
                       <div class="row"><button class="ui-btn" name="op" value="status">Check</button><button class="ui-btn" name="op" value="pull" hx-confirm="Pull the ticked folders (all {len(pull)} its pull list covers when none are ticked) from {UI.escape(target)}? Rule: pull {t.get("pull", "replace")}{" - each folder becomes exactly the target's version (what is here is kept as a backup branch)" if t.get("pull", "replace") == "replace" else ""}.">&#x2193; Pull</button><button class="ui-btn" name="op" value="push" hx-confirm="Push the ticked folders (all {len(push)} its push list covers when none are ticked) to {UI.escape(target)}? Rule: push {t.get("push", "force")}{" - the target gets exactly what is here (what it had is kept here as a branch)" if t.get("push", "force") == "force" else ""}{"; history snapshot - one new commit without history" if t.get("history") == "snapshot" else ""}.">&#x2191; Push</button><span class="htmx-indicator status-label">working...</span><span class="status-label" hx-get="{_P}/sync/progress" hx-trigger="every 1s [document.querySelector('#dev-sync form.sync-run')?.classList.contains('htmx-request')]" hx-target="this" hx-swap="innerHTML"></span></div>
                       {_create_btn(target, results)}
                       {f'<div class="{"status-label" if ok else "err-box"}">{UI.escape(note)}</div>' if note else ""}{after}
                   </form><div hx-get="{_P}/cleanup" hx-trigger="load" hx-swap="outerHTML"></div>{_sync_config_html(note, ok, typed)}</div>"""

# --- Clean up: what saves and syncs left behind (backup copies, the trash, sync's backup branches) ---

def _kept_files() -> list:
    """Backup files and trashed files, oldest first: [{"kind", "path" (under the code root, or backups/... in Dev Studio's data), "size", "at"}]. .bak files beside the code come from older versions of Dev Studio."""
    found = [("backup copy", p, f"backups/{p.relative_to(BACKUPS).as_posix()}") for p in BACKUPS.rglob("*.bak")] if BACKUPS.exists() else []
    found += [("backup copy beside the code", p, p.relative_to(CODE_EDITOR_ROOT).as_posix()) for p in CODE_EDITOR_ROOT.rglob("*.bak") if ".git" not in p.parts and BACKUPS.resolve() not in p.resolve().parents]
    found += [("in the trash", p, p.relative_to(CODE_EDITOR_ROOT).as_posix()) for t in CODE_EDITOR_ROOT.rglob(".trash") if t.is_dir() and ".git" not in t.parts for p in t.iterdir()]
    return sorted(({"kind": k, "path": rel, "size": UI.human_size(p), "at": p.stat().st_mtime} for k, p, rel in found), key=lambda x: x["at"])

def _kept_path(rel: str) -> pathlib.Path:
    p = (DATA_DIR / rel).resolve() if rel.startswith("backups/") else resolve_path(rel)
    if not (p.suffix == ".bak" or ".trash" in p.parts) or (rel.startswith("backups/") and BACKUPS.resolve() not in p.parents): raise HTTPException(status_code=400, detail=f"not a backup or a trashed file: {rel}")
    return p

def _cleanup_html(note: str = "", ok: bool = True) -> str:
    """Every backup file, trashed file and sync backup branch, with its age; nothing is ticked. "Tick older than" ticks by age; Remove asks once more."""
    files, branches = _kept_files(), _gs()["kept"]() if "kept" in _gs() else []
    day = lambda t: (time.time() - t) / 86400
    fr = "".join(f"""<tr data-age="{day(x["at"]):.1f}"><td><input type="checkbox" name="files" value="{UI.escape(x["path"])}"></td><td>{UI.escape(x["kind"])}</td><td class="mono" style="overflow-wrap:anywhere">{UI.escape(x["path"])}</td><td>{x["size"]}</td><td>{day(x["at"]):.0f} d</td></tr>""" for x in files)
    br = "".join(f"""<tr data-age="{day(datetime.fromisoformat(x["at"]).timestamp()) if x["at"] else 0:.1f}"><td><input type="checkbox" name="branches" value="{UI.escape(x["folder"] + "|" + x["branch"])}"></td><td>sync branch</td><td class="mono" style="overflow-wrap:anywhere">{UI.escape(x["folder"])} - {UI.escape(x["branch"])}</td><td>{x["commits"]} commit(s) not in the folder now{"" if x["commits"] else " (nothing lost)"}</td><td>{UI.escape(x["at"][:10])}</td></tr>""" for x in branches)
    tick = """<script>window.devTickOlder = window.devTickOlder || function(f){var d=parseFloat(f.querySelector('[name=older]').value||'0'); f.querySelectorAll('tr[data-age]').forEach(function(r){var c=r.querySelector('input[type=checkbox]'); if(c) c.checked = parseFloat(r.dataset.age) >= d;});};</script>"""
    msg = f"""<div class="{"status-label" if ok else "err-box"}">{UI.escape(note)}</div>""" if note else ""
    return f"""<details id="dev-cleanup" class="glass" style="padding:.6rem" open><summary><b>Clean up</b> <span class="status-label">{len(files)} file(s), {len(branches)} sync branch(es) kept as backups</span></summary>{msg}
               <form hx-post="{_P}/cleanup" hx-target="#dev-cleanup" hx-swap="outerHTML" hx-confirm="Remove what is ticked? Backup copies and trashed files are deleted; a sync branch's commits stay in git's reflog until git prunes it.">
               <div class="row"><span class="status-label">tick everything older than</span><input name="older" type="number" min="0" value="30" class="module-select" style="width:5em"><span class="status-label">days</span><button type="button" class="ui-btn" onclick="devTickOlder(this.closest('form'))">Tick</button><span class="grow"></span><button class="ui-btn">Remove ticked</button></div>
               <div class="scroll-pane" style="max-height:45vh"><table class="data-table" style="width:100%"><tr><th></th><th>kind</th><th>what</th><th>size</th><th>age</th></tr>{fr}{br or ""}</table></div>{"" if fr or br else '<div class="status-label">Nothing kept: no backup copies, trash or sync backup branches.</div>'}</form>{tick}</details>"""

def _sync_mine_html(request: Request, target: str) -> str:
    """What the person acting chooses for themselves: their commit identity, and which of their own Git Manager connections (their token) this target uses when they act - the target's own when none is chosen."""
    gs, user, admin = _gs(), getattr(request.state.user, "username", ""), getattr(request.state.user, "role", "") == "admin"
    me, cfg = gs["mine"](user), gs["config"]()
    ident = me.get("identity") or cfg.get("users", {}).get(user) or {}
    chosen = (me.get("connections") or {}).get(target, "")
    own = cfg["targets"][target].get("connection_id", "")
    opts = f"""<option value="">the target's own ({UI.escape(own)})</option>""" + "".join(f"""<option value="{UI.escape(c["id"])}"{" selected" if c["id"] == chosen else ""}>{UI.escape(c["label"])} - {UI.escape(c["user"])} @ {UI.escape(c["url"][:30])}</option>""" for c in gs["connections"](user, admin) if c["owner"] == user)
    return f"""<details class="sec"><summary>You: {UI.escape(ident.get("name") or "no commit identity yet")}{" &lt;" + UI.escape(ident["email"]) + "&gt;" if ident.get("email") else ""} &#xB7; on {UI.escape(target)} as {UI.escape(chosen or "the target's connection")}</summary><form hx-post="{_P}/sync/mine" hx-target="#dev-sync" hx-swap="outerHTML" class="row" style="flex-wrap:wrap;gap:.4rem"><input type="hidden" name="target" value="{UI.escape(target)}"><input class="module-select" name="name" value="{UI.escape(ident.get("name", ""))}" placeholder="name on commits" style="width:auto"><input class="module-select" name="email" value="{UI.escape(ident.get("email", ""))}" placeholder="email on commits" style="width:auto"><label class="status-label">Connection for {UI.escape(target)} <select name="connection_id" class="module-select" style="width:auto">{opts}</select></label><button class="ui-btn">Save mine</button></form><div class="status-label">Your own connections are made on the Git Manager page (your account and token on that server). A target's own identity, when its config gives one, still signs what is pushed to it.</div></details>"""

def _create_btn(target: str, results: dict) -> str:
    """After a Check or Push that found repositories missing on the target: one button that creates them there (Git Manager, through the server's API) and pushes those folders."""
    missing = [p for p, r in results.items() if r.startswith(f"missing on {target} - Create")]
    if not missing: return ""
    return f"""<div class="row" style="align-items:center"><button type="button" class="ui-btn" hx-post="{_P}/sync/run" hx-target="#dev-sync" hx-swap="outerHTML" hx-include="closest form" hx-vals='{{"op": "push", "create": "1", "only_missing": "1"}}' hx-confirm="Create {len(missing)} repositor{"y" if len(missing) == 1 else "ies"} on {UI.escape(target)} and push those folders?">Create &amp; push {len(missing)} missing</button><span class="status-label">{UI.escape(", ".join(missing))}</span></div>"""

def _sync_grid_html(c: dict) -> str:
    """Which folder goes to and comes from which target, as ticks: one row per folder any list names, a push and a pull tick per target, and per target whether folders added later come along by themselves. A folder a target cannot take (not on the list it is like, not its own) has no ticks there."""
    g = _gs()["grid"](c)
    if not g["targets"]: return ""
    ts = list(g["targets"])
    tick = lambda n, op, p: f"""<input type="checkbox" name="{UI.escape(n)}|{op}" value="{UI.escape(p)}"{" checked" if p in g["targets"][n][op] else ""} title="{op} {UI.escape(p)} {"to" if op == "push" else "from"} {UI.escape(n)}">""" if p in g["targets"][n]["can"] else ""
    head = "".join(f"""<th colspan="2" style="text-align:center">{UI.escape(n)}<br><select name="{UI.escape(n)}|white" class="module-select" style="width:auto;font-size:.7rem" title="When a folder is added to the list this target is like"><option value="1"{" selected" if g["targets"][n]["white"] else ""}>new folders stay off</option><option value=""{"" if g["targets"][n]["white"] else " selected"}>new folders come along</option></select></th>""" for n in ts)
    sub = "".join("""<th class="status-label">push</th><th class="status-label">pull</th>""" for _ in ts)
    rows = "".join(f"""<tr><td class="mono" style="font-size:.72rem">{UI.escape(p)}</td><td class="status-label">{UI.escape(r)}</td>{"".join(f"<td>{tick(n, 'push', p)}</td><td>{tick(n, 'pull', p)}</td>" for n in ts)}</tr>""" for p, r in g["folders"].items())
    return f"""<details class="sec"><summary>Folders by target (ticks)</summary><div class="status-label">What each target is sent and what is taken from it. Saving writes each target's lists as names (a pattern written by hand in a target's lists is replaced); everything else in the config stays as it is. A folder missing here is added to a list in the config below first.</div>
               <form hx-post="{_P}/sync/grid" hx-target="#dev-sync" hx-swap="outerHTML"><div class="scroll-pane" style="max-height:60vh"><table class="data-table"><tr><th rowspan="2">Folder</th><th rowspan="2">Repository</th>{head}</tr><tr>{sub}</tr>{rows}</table></div><div class="form-actions"><button class="ui-btn">Save these lists</button></div></form></details>"""

def _sync_config_html(note: str = "", ok: bool = True, typed: str = None) -> str:
    """The config as text. typed: what was sent and refused, shown again as it was so nothing written is lost."""
    gs = _gs()
    text = typed if typed is not None else json.dumps(gs["config"](), indent=2)
    problems = gs["check_config"](gs["config"]()) if typed is None and text != "{}" else []
    return f"""{_sync_grid_html(gs["config"]())}<details class="sec"{" open" if typed is not None else ""}><summary>Config as text ({UI.escape(gs["config_path"])}){" - " + str(len(problems)) + " problem(s)" if problems else ""}{" - not saved, your text is below as you sent it" if typed is not None else ""}</summary>
               {"".join(f'<div class="err-box">{UI.escape(x)}</div>' for x in problems)}{_json_near(text, note) if typed is not None else ""}
               <div class="status-label">Git Manager connections (set them up on the Git Manager page; use the id as a target's connection_id): {"; ".join(f"{UI.escape(c['id'])} = {UI.escape(c['label'])} ({UI.escape(c['url'])}, user {UI.escape(c['user'])})" for c in gs["connections"]()) or "none yet"}</div>
               <form hx-post="{_P}/sync/config" hx-target="#dev-sync" hx-swap="outerHTML"><textarea name="config" class="cm-input mono" rows="22" style="width:100%;box-sizing:border-box;font-size:.72rem">{UI.escape(text)}</textarea><div class="form-actions"><button class="ui-btn">Save config</button></div></form></details>"""

def _json_near(text: str, note: str) -> str:
    """The lines around the place a JSON error names, numbered, the named line marked."""
    found = re.search(r"line (\d+) column (\d+)", note)
    if not found: return ""
    n, lines = int(found[1]), text.splitlines()
    return f"""<pre class="err-box mono" style="font-size:.72rem;white-space:pre-wrap">{"".join(UI.escape(f"{'>' if i == n else ' '}{i:4} {lines[i - 1]}") + chr(10) for i in range(max(1, n - 3), min(len(lines), n + 2) + 1))}</pre>{'<div class="status-label">Often this line or the one above it is missing a comma, or has one too many.</div>' if "delimiter" in note else ""}"""

@router.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request, target: str = ""): return HTMLResponse(_sync_page(request, target))

@router.post("/sync/run", response_class=HTMLResponse)
async def sync_run(request: Request):
    form = await request.form()
    target, op = str(form.get("target", "")), form.get("op") if form.get("op") in ("status", "pull", "push") else "status"
    paths = [p for p in form.getlist("paths")]
    if form.get("only_missing"): paths = [p for p, r in (_gs()["last"]().get("results") or {}).items() if r.startswith(f"missing on {target} - Create")]   # the Create & push button: only the folders the last run found missing
    try: out = await _gs()["run"](op, target, paths, request.state.user, str(form.get("message", "")).strip(), create=bool(form.get("create")))
    except ValueError as e: return HTMLResponse(_sync_page(request, target, note=str(e), ok=False))
    return HTMLResponse(_sync_page(request, target, out, f"""{op}: {len(out["results"])} folder(s){" - restarting the portal in 2 s; reload in a few seconds" if out["restart"] else ""}"""))

@router.post("/sync/mine", response_class=HTMLResponse)
async def sync_mine(request: Request):
    f = await request.form()
    target = str(f.get("target", ""))
    try: note = _gs()["set_mine"](getattr(request.state.user, "username", ""), getattr(request.state.user, "role", "") == "admin", target, str(f.get("connection_id", "")), str(f.get("name", "")), str(f.get("email", "")))
    except ValueError as e: return HTMLResponse(_sync_page(request, target, note=str(e), ok=False))
    return HTMLResponse(_sync_page(request, target, note=f"yours {note}"))

@router.get("/sync/progress", response_class=HTMLResponse)
async def sync_progress(request: Request):
    pr = _gs()["progress"]()
    return HTMLResponse(f"""{pr["op"]} {pr["done"]} of {pr["total"]}{" - now " + UI.escape(", ".join(pr["current"][:3])) if pr.get("current") else ""} ({round(time.time() - pr["started"])} s)""" if pr else "")

@router.post("/sync/config", response_class=HTMLResponse)
async def sync_config(request: Request):
    if getattr(request.state.user, "role", "") != "admin": return HTMLResponse(_sync_page(request, note="only an admin changes the sync config", ok=False))
    text = str((await request.form()).get("config", ""))
    problems = _gs()["save_config"](text)
    return HTMLResponse(_sync_page(request, note="config saved" if not problems else "not saved: " + "; ".join(problems), ok=not problems, typed=None if not problems else text))

@router.post("/sync/grid", response_class=HTMLResponse)
async def sync_grid(request: Request):
    """The folders-by-target ticks written into each target's lists."""
    if getattr(request.state.user, "role", "") != "admin": return HTMLResponse(_sync_page(request, note="only an admin changes the sync config", ok=False))
    form, gs = await request.form(), _gs()
    c = gs["config"]()
    for n in gs["targets"](c): c = gs["set_lists"](c, n, form.getlist(f"{n}|push"), form.getlist(f"{n}|pull"), bool(form.get(f"{n}|white")))
    problems = gs["save_config"](json.dumps(c, indent=2))
    return HTMLResponse(_sync_page(request, note="lists saved" if not problems else "not saved: " + "; ".join(problems), ok=not problems))

