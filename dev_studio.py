# /modules/dev_studio/router.py
"""
Note: the tab id is not the path, active is tab id not the path.
"""

# import httpx
# from contextlib import contextmanager
import os, subprocess, shutil, pathlib, uuid, json, sqlite3
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

# Change backup location to data path
def backup_before_write(p: pathlib.Path):
    if p.exists(): shutil.copy2(p, DATA_DIR / p.with_name(f"{p.name}.{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.bak"))

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
    if not path or path.startswith("untitled"): return HTMLResponse(_LAUNCHER_HTML)
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
    IM = ENV["InterfaceManager"](nesting_level = 1, db_path = "dev_studio_im_registry.db")
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
        "extra_css": studio_style, "extra_script": ENV["tools"]["built_ins"].PORTAL_EDITOR_JS})

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

@router.post("/cleanup")
async def cleanup_bak_files():
    trash = CODE_EDITOR_ROOT / ".trash"; trash.mkdir(exist_ok=True)
    removed = []
    for bak in CODE_EDITOR_ROOT.rglob("*.bak"):
        try: shutil.move(str(bak), str(trash / f"{bak.name}.{uuid.uuid4().hex[:6]}")); removed.append(bak.name)
        except Exception as e: print(f"[cleanup] {bak}: {e}")
    msg = f"&#x2705; Moved {len(removed)} .bak file(s) to .trash." if removed else "No .bak files found."
    return HTMLResponse(f'<div style="padding:3rem 4rem;"><p style="color:var(--accent);">{msg}</p></div>')

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
        resp = imr.build(f"&#x2705; {meta['modified']}")
        # resp.headers["HX-Trigger"] = json.dumps({"launcherRefresh": True})
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
######################################################################

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
    # elif mode == "git":
    #     content = f'<div style="flex:1;min-height:0;overflow-y:auto;">{gm_render_panel(request, CODE_EDITOR_ROOT)}</div>'
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
        <div style="padding:0.25rem 0.5rem;flex-shrink:0;display:flex;justify-content:space-between;align-items:center;background:var(--surface);border-bottom:var(--board-thick) solid var(--border);">
            <span style="color:var(--accent);font-size:.78rem;">Logs &mdash; <span style="color:var(--text_muted);">{UI.escape(server_name)}/{UI.escape(cid)}</span> <span style="font-size:0.6rem;opacity:0.6;">[{UI.escape(log_source)}]</span></span>
            <button class="ui-btn" style="font-size:0.65rem;padding:0.1rem 0.3rem;" hx-get="{_P}/debug_logs" hx-target="#debug-logs-inner" hx-swap="innerHTML">&#x21BA;</button>
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

# --- Git Status (left-panel git mode) ---

@router.get("/git/status")
async def git_status(request: Request = None):
    cfg, repos, rows = _git_cfg(), _repo_map(), ""
    for repo in repos:
        cwd     = _repo_git_dir(repo)
        has_git = (cwd / ".git").exists()
        rc_b, branch     = _git_run(["rev-parse","--abbrev-ref","HEAD"], cwd) if has_git else (-1,"not initialised")
        rc_s, status_out = _git_run(["status","--short"], cwd) if has_git else (-1,"")
        status_txt = UI.escape(status_out.strip()) if rc_s == 0 and status_out.strip() else "Clean"
        rows += f'<div style="margin-bottom:0.8rem;font-size:0.8rem;"><div style="color:var(--accent);font-weight:600;">{UI.escape(repo["label"])}</div><div style="font-family:var(--font-mono);font-size:0.7rem;color:var(--text_muted);">branch: {UI.escape(branch)}</div><pre style="font-size:0.7rem;margin:0.2rem 0;white-space:pre-wrap;">{status_txt}</pre></div>'
    cfg_note = '<div style="font-size:0.75rem;color:var(--text_muted);margin-bottom:0.8rem;">&#x26A0; Git not configured - open the &#x2601; tab to connect.</div>' if not cfg else ""
    return HTMLResponse(f'<div style="padding:0.8rem;">{cfg_note}{rows}</div>')
