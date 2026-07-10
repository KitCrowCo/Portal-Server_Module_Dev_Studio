# modules/dev_studio/router.py
"""
Note: the tab id is not the path, active is tab id not the path. 
"""
import os, subprocess, shutil, pathlib, uuid, json, sqlite3
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
import httpx
from contextlib import contextmanager

from modules.dev_studio.visual_editor import router as visual_router, visual_editor_root

CODE_EDITOR_ROOT = pathlib.Path(os.getenv("CODE_EDITOR_ROOT", os.getcwd())).resolve()
MAX_UPLOAD_BYTES = int(os.getenv("UPLOAD_LIMIT", 2048 * 1024 * 1024))
_PUSH_LOG_PATH   = pathlib.Path("data/git_push_log.json")

MODULE_META = {"label":"Development Studio","icon":"","description":"IDE Suite for Portal Server development","persistence":"single"}

ENV = {"db":None, "auth":None, "templates":None, "theme":{}, "tools":{}, "get_state":None, "set_state":None, "clear_state":None, "send_push":None, "broadcast_push":None, "push_fragment":None}
IM = None
TM = None
UI = None
md_plus_transpiler = None
tab_bar_from_state = None
IMResponse = None
push_to_client = None
_P = "/module/dev_studio"

router = APIRouter()
router.include_router(visual_router, prefix="/visual", tags=["visual-editor"])

# -- Git binary resolution --

_GIT_BIN = shutil.which("git")
if not _GIT_BIN:
    try:
        subprocess.run(["apt-get","install","-y","--no-install-recommends","git"], capture_output=True, timeout=60)
        _GIT_BIN = shutil.which("git") or "git"
        print(f"[dev_studio] git installed at runtime: {_GIT_BIN}")
    except Exception as _e:
        print(f"[dev_studio] WARNING: git not found and auto-install failed: {_e}"); _GIT_BIN = "git"
else:
    print(f"[dev_studio] git found: {_GIT_BIN}")
try:
    for _gcfg in [["config", "--global", "safe.directory", "*"], ["config", "--global", "init.defaultBranch", "main"],["config", "--global", "user.email", "studio@portal.local"], ["config", "--global", "user.name", "Development Studio"]]:
        subprocess.run([_GIT_BIN]+_gcfg, capture_output=True, timeout=5)
    print("[dev_studio] git global config set")
except Exception as _e:
    print(f"[dev_studio] WARNING: git global config failed: {_e}")

# --- State ---

async def dev_state(request=None, editor=None):
    """Load (editor=None) or save (editor=dict) studio state from user scope."""
    if editor is not None:
        await ENV["set_state"](request, editor, scope="user", namespace="_dev")
        return editor
    editor = await ENV["get_state"](request, scope="user", namespace="_dev") or {}
    editor.setdefault("tabs", {})

    # ensures that the neccesary keys are present (may be removed once properly running) ************************************************
    if editor["tabs"] != {}:   
        for t in editor["tabs"]:
            if not editor["tabs"][t].get("label"): editor["tabs"][t]["label"] = editor["tabs"][t].get("path", "tab")
        await ENV["set_state"](request, editor, scope="user", namespace="_dev")
    # *********************************

    editor.setdefault("active", None)
    editor.setdefault("layout", {})
    editor.setdefault("launcher", {"mode":"files", "cwd":""})
    return editor

# --- Path / File Helpers ---

def resolve_path(rel_path: str) -> pathlib.Path:
    if rel_path in (None, "", "."): return CODE_EDITOR_ROOT
    p = (CODE_EDITOR_ROOT / rel_path).resolve()
    try: p.relative_to(CODE_EDITOR_ROOT)
    except Exception as e: raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    return p

def backup_before_write(p: pathlib.Path):
    if p.exists(): shutil.copy2(p, p.with_name(f"{p.name}.{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.bak"))

def ensure_parent_exists(p: pathlib.Path): p.parent.mkdir(parents=True, exist_ok=True)
def get_file_meta_info(p: pathlib.Path) -> dict: return {"size": UI.human_size(p), "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%H:%M:%S")}
def safe_read_text(p: pathlib.Path) -> str:
    with p.open("r", encoding="utf-8", errors="surrogateescape") as f: return f.read()

# --- Style ---

#studio-content-wrap { display:flex; flex-direction:column; height:100%; overflow:hidden; }
#studio-tab-bar-wrap { flex-shrink:0; }
#editor-stack { flex:1; min-height:0; overflow:hidden; }

old ="""
.CodeMirror { background:var(--bg_panel) !important; color:#d4d4d4 !important; height:100% !important; }
.CodeMirror-cursor { border-left-color:var(--accent) !important; }
.CodeMirror-gutters { background:var(--bg) !important; border-right:var(--border-thick) solid var(--border) !important; }
.cm-s-default .cm-keyword { color:#7ecbca !important; }
.cm-s-default .cm-comment { color:#6a9955 !important; font-style:italic; }
"""

studio_style = """
.CodeMirror-selected {background:rgba(100,180,255,0.2) !important;}
.CodeMirror-linenumber {color:var(--text_muted) !important;}
.cm-s-default .cm-type    {color:#4ec9b0 !important;}
.cm-s-default .cm-string  {color:#7600bc !important;}
.cm-s-default .cm-number  {color:#b5cea8 !important;}
.cm-s-default .cm-def     {color:#9cdcfe !important;}
.cm-s-default .cm-variable{color:#c8e0f0 !important;}
.cm-s-default .cm-builtin {color:#4ec9b0 !important;}
.cm-s-default .cm-atom    {color:#a8d8a8 !important;}

.tree-node summary { display:flex; align-items:center; gap:0.5rem; cursor:pointer; list-style:none; }
.tree-leaf { display:flex; align-items:center; gap:0.5rem; cursor:pointer; padding:0.2rem; }
.tree-menu { margin-left:auto; opacity:0.4; cursor:pointer; }
.tree-node[open] > summary .tree-icon { transform:rotate(90deg); }
.tree-icon { display:inline-block; transition:transform 0.2s ease; }
.tree-leaf:hover, .tree-node summary:hover { background:var(--accent_dim); color:var(--accent); }
.tab.active { background:var(--surface_bright,var(--bg)) !important; border-bottom:2px solid var(--accent) !important; }
#studio-content-wrap { height:100%; overflow:hidden; }
#editor-stack { height:100%; overflow:hidden; }
"""

# Minimal studio JS - only drag-drop tab reorder requires JS with no HTMX equivalent.
# CodeMirror init lives per-file in extra_script of each level-2 file shell.
studio_script = """
htmx.config.allowScriptTags = true;
let dragSrc = null;
document.addEventListener('dragstart', e => { const t = e.target.closest('.tab'); if (t && t.id) dragSrc = t.id.slice(4); });
document.addEventListener('dragover',  e => { if (e.target.closest('.tab')) e.preventDefault(); });
document.addEventListener('drop', e => {
    const t = e.target.closest('.tab');
    const dst = t && t.id ? t.id.slice(4) : null;
    if (dst && dragSrc && dst !== dragSrc)
        htmx.ajax('POST','/im/in',{values:{type:'_dev_studio_reorder',from:dragSrc,to:dst},target:document.body,swap:'none'});
    dragSrc = null;
});
"""

# --- IM Intent Handlers ---

async def _intent_persist_wip(request, payload, imr):
    """Store in-editor content/scroll/cursor without triggering UI refresh."""
    tab_id = payload.get("id", "")
    editor = await dev_state(request)
    if tab_id in editor.get("tabs", {}):
        tab = editor["tabs"][tab_id]
        tab["wip_content"] = payload.get("content")
        try:    tab["wip_scroll"] = float(payload.get("scroll", 0))
        except: tab["wip_scroll"] = None
        try:    tab["wip_cursor"] = json.loads(payload.get("cursor", "null"))
        except: tab["wip_cursor"] = None
        await dev_state(request, editor)
    return imr  # no OOB - silent call

async def _intent_revert(request, payload, imr):
    """Discard WIP for one tab and re-render the file from disk."""
    tab_id = payload.get("id","")
    editor = await dev_state(request)
    if tab_id in editor.get("tabs", {}):  # tab must inherently exist for this or else there is nothing to revert
        tab = editor["tabs"][tab_id]
        tab.pop("wip_content", None); tab.pop("wip_scroll", None); tab.pop("wip_cursor", None)
        tab["dirty"] = False
        await dev_state(request, editor)
        imr.oob((await _render_file_page(request, tab["path"], editor)).body.decode(), "editor-stack")
        imr.oob(await tab_bar_from_state(editor, "studio-tab-bar-wrap", "_dev_studio"), "studio-tab-bar-wrap")
    return imr

async def _intent_reorder(request, payload, imr):
    """Drag-drop tab reorder - mutates state and order fields, re-renders tab bar."""
    editor = await dev_state(request)
    src, dst = payload.get("from",""), payload.get("to","")
    keys = list(editor["tabs"].keys())
    if src in keys and dst in keys:
        si, di = keys.index(src), keys.index(dst)
        keys.insert(di, keys.pop(si))
        editor["tabs"] = {k: editor["tabs"][k] for k in keys}
        for i, k in enumerate(keys): editor["tabs"][k]["order"] = i
    await dev_state(request, editor)
    imr.oob(await tab_bar_from_state(editor, "studio-tab-bar-wrap", "_dev_studio"), "studio-tab-bar-wrap")
    return imr

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

    if ext in (".png",".jpg",".jpeg",".gif",".svg",".webp"):
        return HTMLResponse(f"""<div style="height:100%;display:flex;align-items:center;justify-content:center;padding:1rem;"><img src="{_P}/raw?path={path}" style="max-width:100%;max-height:100%;object-fit:contain;"></div>""")

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

    revert_btn = (f"""<button class="ui-btn" style="color:#ffaa44;padding:0.1rem 0.4rem;font-size:0.75rem;" hx-post="/im/in" hx-swap="none" hx-vals='{json.dumps({"type":"_dev_studio_revert","id":tab_id})}'>&#x21A9;</button>""") if has_wip else ""
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
        htmx.ajax('POST','/im/in',{{values:{{type:'_dev_studio_persist_wip',id:'{tab_id}',content:area.value,scroll:String(area.cm.getScrollInfo().top),cursor:JSON.stringify(area.cm.getCursor())}},swap:'none',target:document.body}});
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
    <div style="display:flex;align-items:center;gap:0.4rem;padding:0.15rem 0.5rem;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--bg_panel);">
    <span style="font-size:0.72rem;color:var(--text_muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{UI.escape(path)}">{UI.escape(path)}{dirty_dot}</span>{save_btn}{revert_btn}</div>
    <form id="form-{safe_id}" style="flex:1;min-height:0;overflow:hidden;">
    <textarea id="editor-{safe_id}" name="content" data-mode="{cm_mode}" data-path="{UI.escape(path)}" data-tab-id="{UI.escape(tab_id)}" data-wip-scroll="{scroll_val}" data-wip-cursor="{cursor_val}" style="display:none;">{UI.escape(content)}</textarea><input type="hidden" name="path" value="{UI.escape(path)}"><input type="hidden" name="tab_id" value="{UI.escape(tab_id)}"></form><script>{file_script}</script></div>""")

# async def _render_file_page(request: Request, path: str, state: dict = None) -> HTMLResponse:
#     """Render a file into a level-2 base.html shell fragment. Note: this is path and not active which is the tab id"""
#     if not path or path.startswith("untitled"): return HTMLResponse(_LAUNCHER_HTML)
#     if path.startswith("design://"): return await visual_editor_root(path.replace("design://",""))
#     if path.startswith("console://"): return HTMLResponse(f'<div style="padding:2rem;color:#ff5f5f;">Console Needs to be reintegrated</div>') #****************************************
#     if not state: state = await dev_state(request)

#     p = resolve_path(path)
#     ext = p.suffix.lower()
#     tab = state["tabs"].get(state["active"])

#     # -- Non-text files --
#     if ext in (".png",".jpg",".jpeg",".gif",".svg",".webp"):
#         return HTMLResponse(f"""<div class="app-shell" data-shell="2" style="height:100%; width:100%; position:relative; grid-area:center;"><div class="content" style="display:flex; align-items:center; justify-content:center; padding:1rem;"><img src="{_P}/raw?path={path}" style="max-width:100%; max-height:100%; object-fit:contain;"></div></div>""")

#     if ext == ".csv":
#         try:
#             rows = [r.split(",") for r in safe_read_text(p).splitlines() if r.strip()]
#             body = UI.table(rows[0], rows[1:]) if len(rows) > 1 else "<p>Empty CSV</p>"
#         except Exception as e: body = f'<p style="color:#ff5f5f;">Error: {UI.escape(str(e))}</p>'
#         return HTMLResponse(f'<div class="app-shell" data-shell="2" style="height:100%; width:100%; position:relative; grid-area:center;"><div class="content" style="overflow:auto; padding:1rem;">{body}</div></div>')

#     # -- Text/code files --
#     # try: content = tab.get("wip_content") or safe_read_text(p)
#    # try:
#     content = safe_read_text(p)
# #    except Exception as e: return HTMLResponse(f'<div style="padding:2rem;color:#ff5f5f;">Error reading file: {UI.escape(str(e))}</div>')

#     cm_mode = _CM_MODES.get(ext[1:], "text/plain")
#     safe_id = path.replace("/","_").replace(".","_").replace("-","_")

#     # Per-file CM init - reads wip state from data attributes to avoid JSON embedding in script strings
#     scroll_val = UI.escape(str(tab.get("wip_scroll","") or ""))
#     cursor_val = UI.escape(json.dumps(tab.get("wip_cursor")) if tab.get("wip_cursor") else "")
#     file_script = ""
# # f"""(function(){{var area=document.getElementById('editor-{safe_id}');if(!area||area.cm)return;area.cm=CodeMirror.fromTextArea(area, {{lineNumbers:true,theme:'default', mode:'{cm_mode}', indentUnit:4}});function sz(){{var p=area.closest('.content'); if(p)area.cm.setSize('100%',p.clientHeight+'px');}}requestAnimationFrame(sz); window.addEventListener('resize',sz); area.cm.on('change',function(){{if(area._ct)clearTimeout(area._ct); area._ct=setTimeout(function(){{area.cm.save(); htmx.ajax('POST','/im/in', {{values:{{type:'_dev_studio_persist_wip', path:area.dataset.path, content:area.value,scroll:String(area.cm.getScrollInfo().top), cursor:JSON.stringify(area.cm.getCursor())}},swap:'none',target:document.body}});}}, 600);}}); requestAnimationFrame(function(){{try{{var ws=area.dataset.wipScroll, wc=area.dataset.wipCursor; if(ws)area.cm.scrollTo(0,Number(ws)); if(wc){{var c=JSON.parse(wc); if(c&&c.line!=null)area.cm.setCursor(c);}}area.cm.refresh();}}catch(e){{}}}});}})();"""

# -- Module Integration --

def init_module(environment: dict):
    global ENV, IM, TM, UI, md_plus_transpiler, tab_bar_from_state, IMResponse, push_to_client
    ENV.update(environment)
    UI = ENV.get("templates").env.globals.get("UI")
    md_plus_transpiler = ENV["tools"]["built_ins"].md_plus_transpiler
    tab_bar_from_state = ENV["tools"]["built_ins"].tab_bar_from_state
    IMResponse = ENV["IMResponse"]
    push_to_client = ENV["push_to_client"]

    IM = ENV["InterfaceManager"](nesting_level = 1, db_path = "dev_studio_im_registry.db")
    TM = ENV["tools"]["built_ins"].TabManager(namespace = "_dev", tab_bar_id = "studio-tab-bar-wrap", content_id = "editor-stack", render_content_fn = _render_active_tab_html, intent_prefix = "_dev_studio", IM = IM)

    IM.scripts["_dev_studio_persist_wip"] = [_intent_persist_wip]
    IM.scripts["_dev_studio_revert"] = [_intent_revert]
    IM.scripts["_dev_studio_reorder"] = [_intent_reorder]
    print("Development Studio: environment synchronized.")

# --- Main Page ---

@router.get("/", response_class=HTMLResponse)
async def studio_main(request: Request):
    editor = await dev_state(request)
    editor, file_content_html = await _render_active_tab_html(request, editor)
    tab_bar_html = await tab_bar_from_state(editor, "studio-tab-bar-wrap", "_dev_studio")

    left_bar = f"""<div style="display:flex;flex-direction:column;height:100%;">
        <div style="display:flex;gap:0.2rem;padding:0.3rem;flex-wrap:wrap;border-bottom:1px solid var(--border);">
            {UI.icon_button("&#x1F4C1;", hint="Files", htmx={"post":f"{_P}/launcher_mode","vals":'{"mode":"files"}', "target":"#launcher-panel","swap":"innerHTML"})}
            {UI.icon_button("&#x1F9E9;", hint="Modules", htmx={"post":f"{_P}/launcher_mode","vals":'{"mode":"modules"}', "target":"#launcher-panel","swap":"innerHTML"})}
            {UI.icon_button("&#x7B;&#x7D;", hint="Dictionary", htmx={"post":f"{_P}/launcher_mode","vals":'{"mode":"dict"}', "target":"#launcher-panel","swap":"innerHTML"})}
            {UI.icon_button("&#x2387;", hint="Git", htmx={"post":f"{_P}/launcher_mode","vals":'{"mode":"git"}', "target":"#launcher-panel","swap":"innerHTML"})}
        </div>
        <div style="display:flex;gap:0.2rem;padding:0.3rem;border-bottom:1px solid var(--border);">
            {UI.icon_button("&#x2795;", hint="New file", htmx={"post":f"{_P}/new","target":"body","swap":"none"})}
            <label style="cursor:pointer;">{UI.icon_button("&#x2B06;", hint="Upload")}<input type="file" style="display:none;" hx-post="{_P}/upload" hx-target="#launcher-panel" hx-swap="innerHTML" hx-encoding="multipart/form-data"></label>
        </div>
        <div id="launcher-panel" {UI.htmx_html({"get":f"{_P}/launcher_tree","trigger":"load, launcherRefresh from:body","target":"this"})} style="flex:1;overflow:auto;font-size:0.82rem;"></div>
    </div>"""
    right_bar = f"""<div id="right-sidebar-outer" hx-get="{_P}/right_sidebar/{"gitea" if _gitea_cfg() else "state"}" hx-trigger="load" hx-target="this"></div>"""

    # Need to add in the save all - title and other status parts *****************************************************************
    top_bar_content = f'<div id="studio-tab-bar-wrap" style="height:100%;">{tab_bar_html}</div>'

    return ENV["templates"].TemplateResponse(name = "base.html", request = request, context = {
        "request": request,
        "user": request.state.user,
#        "code_mirror": True,
        "nesting_level": 1,
        "toolbars": {"top": UI.toolbar(side="top", content=top_bar_content, size="3rem", overlay=False, nesting_level=1, start_open=True, locked=True),
                     "left": UI.toolbar(side="left", content=left_bar, size="16rem", overlay=False, nesting_level=1, start_open=False, locked=False, resizable=True),
                     "right": UI.toolbar(side="right", content=right_bar, size="16rem", overlay=False, nesting_level=1, start_open=True, locked=False, resizable=True)},
        "content": f'<div id="studio-content-wrap"><div id="editor-stack">{file_content_html}</div></div>',
        "extra_css": studio_style,
        "extra_script": studio_script})

# --- File Tree / Launcher ---

@router.get("/launcher_tree")
async def launcher_tree(request: Request, editor=None):
    if not editor: editor = await dev_state(request)
    mode = editor["launcher"]["mode"]
    root = resolve_path(editor["launcher"]["cwd"] or "")
    # Tree opens files via _dev_studio_open_tab intent through /im/in
    if mode == "files":  return HTMLResponse(UI.tree(items = root, mode="file", options={"post":"/im/in","target":"body","swap":"none","extra_vals":{"type":"_dev_studio_open_tab", "label":""}}))
    if mode == "dict":   return HTMLResponse(UI.tree(items = editor.get("dict_view", {}), mode="dict", options={"target":"#editor-stack","swap":"innerHTML"}))
    if mode == "git":    return HTMLResponse(f"<div style='padding:1rem;'>{await git_status()}</div>")
    if mode == "modules":return HTMLResponse("<div style='padding:1rem;'>Module templates - coming soon.</div>")
    return HTMLResponse("")

@router.post("/launcher_mode")
async def set_launcher_mode(request: Request, mode: str = Form(...)):
    editor = await dev_state(request)
    editor["launcher"]["mode"] = mode
    await dev_state(request, editor)
    return await launcher_tree(request, editor)

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

@router.get("/raw")
async def get_raw_file(path: str): return FileResponse(resolve_path(path))
    
@router.post("/new")
async def new_file(request: Request, path: str = Form(""), name: Optional[str] = None):
    base = resolve_path(path)
    if not name: name = f"untitled-{uuid.uuid4().hex[:6]}.txt"
    p = base / name
    if p.exists(): raise HTTPException(status_code=400, detail="File exists")
    ensure_parent_exists(p); p.write_text("", encoding="utf-8")
    rel_path = str(p.relative_to(CODE_EDITOR_ROOT)).replace("\\", "/")
    tab_id = f"file-{uuid.uuid4().hex[:6]}"
    imr = await TM._open(request, {"id": tab_id, "path": rel_path, "label": name}, IMResponse())
    resp = imr.build()
    # resp.headers["HX-Trigger"] = json.dumps({"launcherRefresh": True})
    return resp
    
@router.post("/new_folder")
async def new_folder(path: str = "", name: Optional[str] = None):
    base = resolve_path(path)
    if not name: name = f"folder-{uuid.uuid4().hex[:6]}"
    (base / name).mkdir(parents=True, exist_ok=False)
    return HTMLResponse("", headers={"HX-Trigger":"launcherRefresh"})

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

@router.post("/upload")
async def upload_file(path: str = Form(...), file: UploadFile = File(...)):
    folder = resolve_path(path); folder.mkdir(parents=True, exist_ok=True)
    dest = folder / pathlib.Path(file.filename).name; size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(65536):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES: out.close(); dest.unlink(missing_ok=True); raise HTTPException(status_code=413)
            out.write(chunk)
    return HTMLResponse(f"Uploaded: {dest.name}", headers={"HX-Trigger":"launcherRefresh"})

@router.get("/refresh")
async def global_refresh():
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({"launcherRefresh":True})})

# --- Right Sidebar ---

@router.get("/right_sidebar/{mode}")
async def get_right_sidebar(mode: str, request: Request):
    MODES = [("&#x2699;","state"),("&#x1F4CB;","logs"),("&#x2387;","git"),("&#x1F3A8;","palette"),("&#x2753;","help"),("&#x2601;","gitea")]
    valid = {m for _,m in MODES}
    if mode == "debug": mode = "state"
    if mode not in valid: mode = "state"

    if mode == "state":
        content = f'<div style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;"><div id="debug-state-inner" hx-get="{_P}/debug_state" hx-trigger="load" hx-target="this" hx-swap="innerHTML" style="flex:1;min-height:0;overflow:hidden;"><div style="padding:0.4rem;color:var(--text_muted);font-size:0.75rem;">Loading...</div></div></div>'
    elif mode == "logs":
        content = f'<div style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;"><div id="debug-logs-inner" hx-get="{_P}/debug_logs" hx-trigger="load" hx-target="this" hx-swap="innerHTML" style="flex:1;min-height:0;overflow:hidden;"><div style="padding:0.4rem;color:var(--text_muted);font-size:0.75rem;">Loading...</div></div></div>'
    elif mode == "git":
        content = f'<div style="flex:1;min-height:0;overflow-y:auto;"><div hx-get="{_P}/git/status" hx-trigger="load" hx-target="this" hx-swap="innerHTML"><div style="padding:0.4rem;color:var(--text_muted);font-size:0.75rem;">Loading git status...</div></div></div>'
    elif mode == "palette":
        import inspect as _inspect
        items = "".join(f'<div style="padding:0.3rem 0.5rem;font-size:0.8rem;cursor:grab;border-bottom:1px solid var(--border);">{f}</div>' for f in [n for n,_ in _inspect.getmembers(UI, predicate=_inspect.isfunction) if not n.startswith("_")])
        content = f'<div style="flex:1;min-height:0;overflow-y:auto;">{items}</div>'
    elif mode == "help":
        help_path = CODE_EDITOR_ROOT / "tools" / "dev_studio" / "HELP.md"
        if help_path.exists():
            try: content = f'<div style="flex:1;min-height:0;overflow-y:auto;padding:0.8rem;font-size:0.8rem;">{md_plus_transpiler(help_path.read_text(encoding="utf-8",errors="replace"))}</div>'
            except Exception as e: content = f'<div style="padding:1rem;color:#ff5f5f;">Could not load HELP.md: {UI.escape(str(e))}</div>'
        else:
            content = '<div style="flex:1;overflow-y:auto;padding:1rem;font-size:0.8rem;"><p style="color:var(--text_muted);">Place <code>tools/dev_studio/HELP.md</code> for local docs.</p></div>'
    elif mode == "gitea":
        content = await _gitea_panel_html(request)
    else:
        content = '<div style="padding:1rem;color:var(--text_muted);">Unknown mode</div>'

    tabs = "".join(f'<div class="sidebar-tab {"active" if mode==m else ""}" hx-get="{_P}/right_sidebar/{m}" hx-target="#right-sidebar-outer" title="{m}" style="cursor:pointer;padding:0.45rem 0.6rem;font-size:1rem;{"color:var(--accent);" if mode==m else "color:var(--text_muted);"}">{icon}</div>' for icon,m in MODES)
    return HTMLResponse(f'<div id="right-sidebar-outer" style="display:flex;flex-direction:column;height:100%;overflow:hidden;"><div style="display:flex;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--bg_panel);">{tabs}</div><div id="right-sidebar-inner" style="flex:1;height:0;display:flex;flex-direction:column;overflow:hidden;">{content}</div></div>')

# --- Debug ---

@router.get("/debug_state")
async def debug_state(request: Request):
    editor = await dev_state(request)
    active = editor.get("active","none")
    return HTMLResponse(f"""<div style="flex:1;min-height:0;display:flex;flex-direction:column;font-family:monospace;font-size:0.72rem;overflow:hidden;"><div style="padding:0.25rem 0.5rem;flex-shrink:0;display:flex;justify-content:space-between;align-items:center;background:var(--surface);border-bottom:var(--border-thick) solid var(--border);"><span style="color:var(--accent);">State &mdash; <b>{UI.escape(active)}</b></span><span style="display:flex;gap:0.3rem;"><button class="ui-btn" style="font-size:0.65rem;padding:0.1rem 0.3rem;" hx-get="{_P}/debug_state" hx-target="#debug-state-inner" hx-swap="innerHTML">&#x21BA;</button><button class="ui-btn" style="font-size:0.65rem;padding:0.1rem 0.3rem;" hx-post="/im/in" hx-vals='{json.dumps({"type":"_dev_studio_revert","id":active})}' hx-swap="none">&#x21A9; Revert WIP</button></span></div><pre style="flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;white-space:pre-wrap;word-break:break-all;margin:0;padding:0.4rem;">{UI.escape(json.dumps(editor, indent=2))}</pre></div>""")

@router.get("/debug_logs")
async def debug_logs():
    cid, server_name = os.environ.get("HOSTNAME","portal_server"), os.environ.get("SERVER_NAME","portal")
    logs = log_source = ""
    for lpath in ["/tmp/server.log","/proc/1/fd/1"]:
        if logs: break
        try:
            if os.path.exists(lpath):
                r = subprocess.run(["tail","-n","200",lpath], capture_output=True, text=True, timeout=4)
                if r.stdout.strip(): logs = r.stdout; log_source = lpath
        except: pass
    if not logs: logs = f""""Log not yet available.\nServer: {server_name} ({cid})\ngit: {_GIT_BIN or 'NOT FOUND'}\n\nuvicorn ... 2>&1 | tee /tmp/server.log\n"""; log_source = "unavailable"
    return HTMLResponse(f'<div style="flex:1;min-height:0;display:flex;flex-direction:column;font-family:monospace;font-size:0.72rem;overflow:hidden;"><div style="padding:0.25rem 0.5rem;flex-shrink:0;display:flex;justify-content:space-between;align-items:center;background:var(--surface);border-bottom:1px solid var(--border);"><span style="color:var(--accent);">Logs &mdash; <span style="color:var(--text_muted);">{UI.escape(server_name)}/{UI.escape(cid)}</span> <span style="font-size:0.6rem;opacity:0.6;">[{UI.escape(log_source)}]</span></span><button class="ui-btn" style="font-size:0.65rem;padding:0.1rem 0.3rem;" hx-get="{_P}/debug_logs" hx-target="#debug-logs-inner" hx-swap="innerHTML">&#x21BA;</button></div><pre id="log-pre" style="flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;white-space:pre-wrap;word-break:break-all;margin:0;padding:0.4rem;background:var(--bg);">{UI.escape(logs)}</pre></div><script>(function(){{var e=document.getElementById("log-pre");if(e)e.scrollTop=e.scrollHeight;}})();</script>')

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

# --- Database Editor ---

@router.get("/db/open")
async def open_db(path: str):
    p = resolve_path(path)
    if not p.exists(): raise HTTPException(404)
    conn   = sqlite3.connect(p)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    tlist  = "".join(f"<li><a href='javascript:void(0)' onclick=\"loadDbTable('{path}','{t[0]}')\">{t[0]}</a></li>" for t in tables)
    return HTMLResponse(f'<div style="display:flex;gap:1rem;height:80vh;"><div style="width:20%;border-right:1px solid var(--border);padding:1rem;"><h4>Tables</h4><ul>{tlist}</ul></div><div style="flex:1;display:flex;flex-direction:column;"><div id="db-table-view" style="flex:1;overflow:auto;">Select a table.</div><div style="border-top:1px solid var(--border);padding:0.5rem;"><textarea id="db-query-box" style="width:100%;height:6rem;font-family:monospace;"></textarea><button onclick="runDbQuery(\'{path}\')">Run Query</button></div><div id="db-query-result" style="flex:1;overflow:auto;"></div></div></div>')

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
    cfg, repos, rows = _gitea_cfg(), _repo_map(), ""
    for repo in repos:
        cwd     = _repo_git_dir(repo)
        has_git = (cwd / ".git").exists()
        rc_b, branch     = _git_run(["rev-parse","--abbrev-ref","HEAD"], cwd) if has_git else (-1,"not initialised")
        rc_s, status_out = _git_run(["status","--short"], cwd) if has_git else (-1,"")
        status_txt = UI.escape(status_out.strip()) if rc_s == 0 and status_out.strip() else "Clean"
        btn_init   = (f'<button class="ui-btn" style="font-size:0.75rem;" hx-post="{_P}/gitea/init-repo" hx-vals=\'{{"repo_name":"{UI.escape(repo["repo_name"])}","local_path":"{UI.escape(repo["local_path"])}"}}\' hx-target="#launcher-panel">Init + Link</button>') if not has_git else ""
        rows += f'<div style="margin-bottom:0.8rem;font-size:0.8rem;"><div style="color:var(--accent);font-weight:600;">{UI.escape(repo["label"])}</div><div style="font-family:var(--font-mono);font-size:0.7rem;color:var(--text_muted);">branch: {UI.escape(branch)}</div><pre style="font-size:0.7rem;margin:0.2rem 0;white-space:pre-wrap;">{status_txt}</pre>{btn_init}</div>'
    cfg_note = '<div style="font-size:0.75rem;color:var(--text_muted);margin-bottom:0.8rem;">&#x26A0; Gitea not configured - open the &#x2601; tab to connect.</div>' if not cfg else ""
    return HTMLResponse(f'<div style="padding:0.8rem;">{cfg_note}{rows}</div>')

# ============================================================
# GITEA / GITHUB INTEGRATION
# ============================================================

def _gitea_cfg() -> Optional[dict]:
    try:
        conn = sqlite3.connect(pathlib.Path("data/server.db"))
        cfg  = {r[0].replace("gitea_","",1): r[1] for r in conn.execute("SELECT key,value FROM ui_strings WHERE key LIKE 'gitea_%'").fetchall()}
        conn.close()
        return cfg if cfg.get("url") and cfg.get("token") and cfg.get("user") else None
    except: return None

def _github_cfg() -> dict:
    try:
        conn = sqlite3.connect(pathlib.Path("data/server.db"))
        cfg  = {r[0].replace("github_","",1): r[1] for r in conn.execute("SELECT key,value FROM ui_strings WHERE key LIKE 'github_%'").fetchall()}
        conn.close()
        return cfg if cfg.get("token") and cfg.get("user") else {}
    except: return {}

def _gitea_headers(cfg): return {"Authorization": f"token {cfg['token']}", "Content-Type": "application/json"}

async def _gitea_api(method: str, path: str, cfg=None, **kwargs):
    cfg = cfg or _gitea_cfg()
    if not cfg: return 0, {"error": "Gitea not configured"}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await getattr(client, method.lower())(f"{cfg['url']}/api/v1{path}", headers=_gitea_headers(cfg), **kwargs)
            try: data = r.json()
            except: data = {"raw": r.text}
            return r.status_code, data
    except Exception as e: return 0, {"error": str(e)}

def _repo_map() -> list:
    cfg   = _gitea_cfg() or {}
    repos = [{"label":"Portal Server","local_path":".","repo_name":cfg.get("server_repo","portal-server"),"description":"Core portal server","type":"server","gitignore_extras":["data/","modules/","tools/"]}]
    for kind, label_prefix in [("tools","Tool"),("modules","Module")]:
        d = CODE_EDITOR_ROOT / kind
        if d.exists():
            repos += [{"label":f"{label_prefix}: {sub.name}","local_path":str(sub.relative_to(CODE_EDITOR_ROOT)),"repo_name":f"{kind[:-1]}-{sub.name.replace('_','-')}","description":f"Portal {label_prefix.lower()}: {sub.name}","type":kind[:-1],"gitignore_extras":[]} for sub in sorted(d.iterdir()) if sub.is_dir() and not sub.name.startswith(".")]
    return repos

def _repo_git_dir(repo: dict) -> pathlib.Path:
    return CODE_EDITOR_ROOT if repo["local_path"] == "." else CODE_EDITOR_ROOT / repo["local_path"]

def _git_run(args: list, cwd: pathlib.Path, timeout: int = 15):
    try:
        r = subprocess.run([_GIT_BIN]+args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env={**os.environ,"GIT_TERMINAL_PROMPT":"0"})
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError: return -1, "git binary not found"
    except Exception as e:    return -1, str(e)

def _git_remote_url(cfg: dict, repo_name: str) -> str:
    base = cfg["url"].replace("https://","").replace("http://","")
    return f"http://{cfg['user']}:{cfg['token']}@{base}/{cfg['user']}/{repo_name}.git"

def _gh_remote_url(gh_cfg: dict, repo_name: str) -> str:
    return f"https://{gh_cfg['user']}:{gh_cfg['token']}@github.com/{gh_cfg['user']}/{gh_cfg.get(f'repo_{repo_name}',repo_name)}.git"

async def _ensure_repo_exists(cfg: dict, repo: dict):
    status, data = await _gitea_api("GET", f"/repos/{cfg['user']}/{repo['repo_name']}", cfg)
    if status == 200: return False, None
    if status == 404:
        status2, data2 = await _gitea_api("POST", "/user/repos", cfg, json={"name":repo["repo_name"],"description":repo.get("description",""),"private":True,"auto_init":False})
        return (True, None) if status2 == 201 else (False, data2.get("message","Failed to create repo"))
    return False, data.get("message",f"HTTP {status}")

async def _init_git_if_needed(repo: dict):
    cwd = _repo_git_dir(repo)
    def _set_id():
        _git_run(["config","user.email","studio@portal.local"], cwd)
        _git_run(["config","user.name","Development Studio"], cwd)
    if (cwd / ".git").exists(): _set_id(); return True, "already initialised"
    rc, out = _git_run(["init","-b","main"], cwd)
    if rc != 0: return False, out
    _set_id()
    gi = cwd / ".gitignore"
    if not gi.exists(): gi.write_text("*.bak\n__pycache__/\n*.pyc\n.env\ndata/\nassets/\n" + "\n".join(repo.get("gitignore_extras",[])))
    return True, "ok"

def _read_push_log() -> dict:
    try: return json.loads(_PUSH_LOG_PATH.read_text()) if _PUSH_LOG_PATH.exists() else {}
    except: return {}

def _write_push_log(repo_name: str, short_hash: str, message: str):
    log = _read_push_log()
    log[repo_name] = {"time":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"hash":short_hash,"server":os.environ.get("SERVER_NAME",os.environ.get("HOSTNAME","unknown")),"message":(message or "")[:80]}
    try: _PUSH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True); _PUSH_LOG_PATH.write_text(json.dumps(log,indent=2))
    except: pass

def _upsert_ui_strings(pairs: list):
    conn = sqlite3.connect(pathlib.Path("data/server.db"))
    for key, val in pairs:
        if conn.execute("SELECT id FROM ui_strings WHERE key=?", (key,)).fetchone():
            conn.execute("UPDATE ui_strings SET value=? WHERE key=?", (val,key))
        else: conn.execute("INSERT INTO ui_strings (key,value) VALUES (?,?)", (key,val))
    conn.commit(); conn.close()

def _delete_ui_strings(keys: list):
    conn = sqlite3.connect(pathlib.Path("data/server.db"))
    conn.execute(f"DELETE FROM ui_strings WHERE key IN ({','.join('?'*len(keys))})", keys)
    conn.commit(); conn.close()

def _cfg_input(name: str, placeholder: str, password: bool = False) -> str:
    return f'<input name="{name}" type="{"password" if password else "text"}" placeholder="{UI.escape(placeholder)}" autocomplete="{"new-password" if password else "off"}" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:0.45rem;border-radius:var(--radius);font-size:0.8rem;width:100%;box-sizing:border-box;">'

# -- Gitea panel --

async def _gitea_panel_html(request) -> str:
    cfg       = _gitea_cfg()
    gh_cfg    = _github_cfg()
    server_id = os.environ.get("SERVER_NAME", os.environ.get("HOSTNAME","this server"))

    if not cfg:
        return f'<div style="flex:1;overflow-y:auto;padding:1rem;"><div style="color:var(--accent);font-weight:600;margin-bottom:0.8rem;">&#x2601; Gitea - Not Connected</div><p style="color:var(--text_muted);font-size:0.8rem;margin-bottom:1rem;">Enter Gitea details below. Saved to portal database.</p><form hx-post="{_P}/gitea/configure" hx-target="#right-sidebar-inner" hx-swap="innerHTML" style="display:flex;flex-direction:column;gap:0.6rem;">{_cfg_input("url","Gitea URL e.g. http://gitea:3000")}{_cfg_input("user","Gitea username")}{_cfg_input("token","Personal Access Token",password=True)}<button type="submit" class="ui-btn" style="background:var(--accent);color:var(--bg);">Connect</button></form></div>'

    repos, log = _repo_map(), _read_push_log()
    repo_cards = ""
    for repo in repos:
        cwd       = _repo_git_dir(repo)
        has_git   = (cwd / ".git").exists()
        rn, lp    = UI.escape(repo["repo_name"]), UI.escape(repo["local_path"])
        vals      = f'{{"repo_name":"{rn}","local_path":"{lp}"}}'
        rc_b, branch    = _git_run(["rev-parse","--abbrev-ref","HEAD"], cwd) if has_git else (-1,"uninitialised")
        rc_s, dirty_out = _git_run(["status","--short"], cwd) if has_git else (-1,"")
        dirty     = rc_s == 0 and bool(dirty_out.strip())
        dot       = (f'<span style="color:{"#ff9944" if dirty else "var(--accent)"}" title="{"uncommitted changes" if dirty else "clean"}">&#x25CF;</span>') if has_git else '<span style="color:var(--text_muted);" title="not initialised">&#x25CB;</span>'
        push_info = log.get(repo["repo_name"])
        push_label= (f'<span style="font-size:0.65rem;color:{"var(--accent)" if push_info.get("server","") == server_id else "#ffaa44"};">&#x1F4BE; {UI.escape(push_info["time"])} from {UI.escape(push_info["server"])}</span>') if push_info else '<span style="font-size:0.65rem;color:var(--text_muted);">No push recorded yet</span>'
        if not has_git:
            btns = f'<button class="ui-btn" style="font-size:0.7rem;padding:0.15rem 0.5rem;" hx-post="{_P}/gitea/init-repo" hx-vals=\'{vals}\' hx-target="#gitea-op-out">&#x1F517; Init + Link</button>'
        else:
            btns = f'<button class="ui-btn" style="font-size:0.7rem;padding:0.15rem 0.5rem;" hx-post="{_P}/gitea/commit-push" hx-include="#gitea-commit-msg" hx-vals=\'{vals}\' hx-target="#gitea-op-out">&#x2191; Push</button> <button class="ui-btn" style="font-size:0.7rem;padding:0.15rem 0.5rem;" hx-get="{_P}/gitea/pull-info/{rn}/{lp}" hx-target="#gitea-op-out">&#x2193; Pull&#x2026;</button>'
            if gh_cfg: btns += f' <button class="ui-btn" style="font-size:0.7rem;padding:0.15rem 0.5rem;" hx-post="{_P}/gitea/push-github" hx-include="#gitea-commit-msg" hx-vals=\'{vals}\' hx-target="#gitea-op-out">&#x2605; GitHub</button>'
        repo_cards += f'<div style="border:1px solid var(--border);border-radius:var(--radius);padding:0.5rem;margin-bottom:0.5rem;"><div style="display:flex;justify-content:space-between;align-items:center;"><span style="font-weight:600;font-size:0.8rem;">{dot} {UI.escape(repo["label"])}</span><span style="font-size:0.68rem;color:var(--text_muted);font-family:monospace;">{UI.escape(branch)}</span></div><div style="margin:0.1rem 0;">{push_label}</div><div style="display:flex;gap:0.3rem;flex-wrap:wrap;margin-top:0.3rem;">{btns}</div></div>'

    gh_section = (f'<div style="border-top:1px solid var(--border);margin-top:0.5rem;padding-top:0.5rem;"><div style="font-size:0.68rem;color:var(--accent);">&#x2605; GitHub: {UI.escape(gh_cfg.get("user",""))} <button class="ui-btn" style="font-size:0.65rem;padding:0.1rem 0.3rem;" hx-post="{_P}/github/disconnect" hx-target="#right-sidebar-inner">&#x2715;</button></div></div>') if gh_cfg else (f'<div style="border-top:1px solid var(--border);margin-top:0.5rem;padding-top:0.7rem;"><div style="font-size:0.65rem;color:var(--text_muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:0.4rem;">GitHub Mirror (optional)</div><form hx-post="{_P}/github/configure" hx-target="#right-sidebar-inner" hx-swap="innerHTML" style="display:flex;flex-direction:column;gap:0.4rem;">{_cfg_input("gh_user","GitHub username")}{_cfg_input("gh_token","GitHub PAT (repo scope)",password=True)}<button type="submit" class="ui-btn" style="font-size:0.75rem;">Save GitHub</button></form></div>')

    try:
        conn = sqlite3.connect(pathlib.Path("data/server.db"))
        row  = conn.execute("SELECT value FROM ui_strings WHERE key='prod_server_url'").fetchone()
        conn.close(); prod_url = UI.escape(row[0] if row else "")
    except: prod_url = ""

    return f'<div style="flex:1;height:0;display:flex;flex-direction:column;overflow:hidden;"><div style="flex-shrink:0;padding:0.45rem 0.7rem;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--bg_panel);"><span style="font-size:0.78rem;font-weight:600;color:var(--accent);">&#x2601; {UI.escape(cfg.get("url",""))}</span><span style="display:flex;gap:0.3rem;align-items:center;"><span style="font-size:0.65rem;color:var(--text_muted);">{UI.escape(server_id)}</span><button class="ui-btn" style="font-size:0.68rem;padding:0.15rem 0.4rem;" hx-post="{_P}/gitea/disconnect" hx-target="#right-sidebar-inner" hx-swap="innerHTML" hx-confirm="Disconnect?">&#x2715;</button></span></div><div style="flex:1;height:0;overflow-y:scroll;padding:0.6rem;"><div style="font-size:0.65rem;color:var(--text_muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:0.3rem;">Commit message</div><textarea id="gitea-commit-msg" name="gitea-commit-msg" placeholder="Leave blank for auto-generated\u2026" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:0.4rem;border-radius:var(--radius);font-size:0.75rem;min-height:42px;resize:vertical;box-sizing:border-box;margin-bottom:0.6rem;"></textarea><div style="font-size:0.65rem;color:var(--text_muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:0.4rem;">Repositories</div>{repo_cards}{gh_section}<div style="border-top:1px solid var(--border);margin-top:0.5rem;padding-top:0.7rem;"><div style="font-size:0.65rem;color:var(--text_muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:0.4rem;">Production Deploy</div><input id="prod-server-url" name="prod_server_url" value="{prod_url}" placeholder="http://prod-server:8000" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:0.4rem;border-radius:var(--radius);font-size:0.75rem;box-sizing:border-box;margin-bottom:0.4rem;"><button class="ui-btn" style="width:100%;font-size:0.75rem;" hx-post="{_P}/deploy/signal" hx-include="#prod-server-url,#gitea-commit-msg" hx-target="#gitea-op-out">&#x1F680; Signal Deploy</button></div></div><div style="flex-shrink:0;border-top:2px solid var(--border);background:var(--bg);padding:0.4rem;"><div style="font-size:0.6rem;color:var(--text_muted);margin-bottom:0.2rem;">Output</div><div id="gitea-op-out" style="font-family:monospace;font-size:0.72rem;white-space:pre-wrap;max-height:8rem;overflow-y:auto;min-height:1.5rem;"></div></div></div>'

# -- Gitea routes --

@router.get("/right_sidebar/gitea")
async def gitea_sidebar(request: Request): return await get_right_sidebar("gitea", request)

@router.post("/gitea/configure")
async def gitea_configure(request: Request, url: str = Form(...), user: str = Form(...), token: str = Form(...)):
    url = url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{url}/api/v1/user", headers={"Authorization": f"token {token}"})
            if r.status_code != 200: return HTMLResponse(f'<span style="color:#ff5f5f;">Connection failed: HTTP {r.status_code}</span>')
            actual_user = r.json().get("login", user)
    except Exception as e: return HTMLResponse(f'<span style="color:#ff5f5f;">Connection error: {UI.escape(str(e))}</span>')
    try: _upsert_ui_strings([("gitea_url",url),("gitea_user",actual_user),("gitea_token",token)])
    except Exception as e: return HTMLResponse(f'<span style="color:#ff5f5f;">DB error: {UI.escape(str(e))}</span>')
    return HTMLResponse(await _gitea_panel_html(request))

@router.post("/gitea/disconnect")
async def gitea_disconnect(request: Request):
    try: _delete_ui_strings(["gitea_url","gitea_user","gitea_token"])
    except Exception as e: return HTMLResponse(f'Error: {UI.escape(str(e))}')
    return HTMLResponse(await _gitea_panel_html(request))

@router.post("/gitea/init-repo")
async def gitea_init_repo(request: Request):
    form = await request.form()
    repo_name, local_path = form.get("repo_name","").strip(), form.get("local_path","").strip()
    cfg = _gitea_cfg()
    if not cfg:       return HTMLResponse('<span style="color:#ff5f5f;">Gitea not configured.</span>')
    if not repo_name: return HTMLResponse('<span style="color:#ff5f5f;">No repo_name provided.</span>')
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M")
    repo = {"repo_name":repo_name,"local_path":local_path,"gitignore_extras":[]}
    cwd  = _repo_git_dir(repo); steps = []
    ok, msg = await _init_git_if_needed(repo); steps.append(f"git init: {msg}")
    created, err = await _ensure_repo_exists(cfg, repo)
    if err: return HTMLResponse(f'<pre style="color:#ff5f5f;font-size:0.73rem;">{UI.escape(chr(10).join(steps + [f"Gitea repo: ERROR - {err}"]))}</pre>')
    steps.append(f"Gitea repo: {'created' if created else 'already exists'}")
    rc, _ = _git_run(["remote","get-url","origin"], cwd)
    rc2, out2 = _git_run(["remote","add" if rc != 0 else "set-url","origin",_git_remote_url(cfg,repo_name)], cwd)
    steps.append(f"remote: {'set' if rc2==0 else out2.strip()[:80]}")
    _git_run(["config","user.email",f"{cfg.get('user','studio')}@portal.local"], cwd)
    _git_run(["config","user.name", cfg.get("user","Development Studio")], cwd)
    _git_run(["add","-A"], cwd)
    rc_log, _ = _git_run(["log","--oneline","-1"], cwd)
    commit_msg = f"Initial commit - {ts}" if rc_log != 0 else f"WIP checkpoint - {ts}"
    rc, out = _git_run(["commit","--allow-empty","-m",commit_msg], cwd)
    steps.append(f"commit: {out.splitlines()[0] if out.strip() else 'ok'}" if rc == 0 else f"commit: {'clean' if 'nothing to commit' in out else out.strip()[:120]}")
    rc, out = _git_run(["push","-u","origin","main"], cwd)
    if rc != 0: rc, out = _git_run(["push","-u","--force-with-lease","origin","main"], cwd)
    steps.append(f"push: {'╬ô┬ú├┤ ok' if rc==0 else out.strip()[:200]}")
    if rc == 0:
        _, short_hash = _git_run(["rev-parse","--short","HEAD"], cwd)
        _write_push_log(repo_name, short_hash, commit_msg)
    ok_flag = not any(w in " ".join(steps).lower() for w in ("error","fatal","denied"))
    return HTMLResponse(f'<pre style="color:{"var(--accent)" if ok_flag else "#ff9944"};font-size:0.73rem;line-height:1.4;">{UI.escape(chr(10).join(steps))}</pre>')

@router.post("/gitea/commit-push")
async def gitea_commit_push(request: Request):
    form = await request.form()
    repo_name  = form.get("repo_name","")
    local_path = form.get("local_path","")
    commit_msg = (form.get("gitea-commit-msg") or form.get("gitea_commit_msg") or "").strip()
    cfg = _gitea_cfg()
    if not cfg: return HTMLResponse('<span style="color:#ff5f5f;">Gitea not configured.</span>')
    repo = {"repo_name":repo_name,"local_path":local_path}
    cwd  = _repo_git_dir(repo)
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M"); steps = []
    _git_run(["remote","set-url","origin",_git_remote_url(cfg,repo_name)], cwd)
    _git_run(["config","user.email",f"{cfg.get('user','studio')}@portal.local"], cwd)
    _git_run(["config","user.name", cfg.get("user","Development Studio")], cwd)
    rc, out = _git_run(["add","-A"], cwd); steps.append(f"stage: {'ok' if rc==0 else out[:80]}")
    if not commit_msg: commit_msg = f"WIP checkpoint - {ts}"
    rc, out = _git_run(["commit","-m",commit_msg], cwd)
    steps.append(f"commit: {out.splitlines()[0][:80]}" if rc==0 and out else ("nothing new" if "nothing to commit" in out else f"commit note: {out.strip()[:120]}"))
    # Push with escalating force - break on first success
    push_rc = -1
    for push_args, label in [
        (["push","origin","main"],                       "ok"),
        (["push","--force-with-lease","origin","main"],  "ok (force-with-lease)"),
        (["push","--force","origin","main"],              "ok (--force - remote replaced)"),
    ]:
        push_rc, out = _git_run(push_args, cwd)
        if push_rc == 0: steps.append(f"push: ╬ô┬ú├┤ {label}"); break
        steps.append(f"push rejected ({out.strip()[:60]}) - retrying╬ô├ç┬¬")
    else:
        steps.append(f"push: FAILED - {out.strip()[:200]}")
    if push_rc == 0:
        _, short_hash = _git_run(["rev-parse","--short","HEAD"], cwd)
        _write_push_log(repo_name, short_hash, commit_msg)
    return HTMLResponse(f'<pre style="color:{"var(--accent)" if push_rc==0 else "#ff5f5f"};font-size:0.75rem;">{UI.escape(chr(10).join(steps))}</pre>')

@router.get("/gitea/pull-info/{repo_name:path}")
async def gitea_pull_info(repo_name: str, request: Request):
    parts   = repo_name.split("/",1)
    rn, lp  = parts[0], parts[1] if len(parts) > 1 else "."
    cfg     = _gitea_cfg()
    server_id = os.environ.get("SERVER_NAME", os.environ.get("HOSTNAME","this server"))
    log, push_info = _read_push_log(), None
    push_info = log.get(rn)
    repo    = {"repo_name":rn,"local_path":lp}
    cwd     = _repo_git_dir(repo)
    rc_h, local_hash = _git_run(["rev-parse","--short","HEAD"], cwd) if (cwd/".git").exists() else (-1,"none")
    _, local_msg = _git_run(["log","-1","--format=%s %cd","--date=short"], cwd) if rc_h==0 else (-1,"")
    remote_info = "unavailable"
    if cfg:
        try:
            status, data = await _gitea_api("GET", f"/repos/{cfg['user']}/{rn}/commits?limit=1", cfg)
            if status == 200 and data:
                c = data[0] if isinstance(data,list) else data
                sha, cdate, cmsg = (c.get("sha","") or "")[:7], (c.get("commit",{}).get("author",{}).get("date","") or "")[:10], (c.get("commit",{}).get("message","") or "").splitlines()[0][:60]
                remote_info = f"{sha} ({cdate}) - {UI.escape(cmsg)}"
        except: pass
    push_line = (f"Last push: {UI.escape(push_info['time'])} from {UI.escape(push_info['server'])}<br>Message: {UI.escape(push_info.get('message',''))}") if push_info else "No push recorded for this repo"
    warn_color = "#ffaa44" if push_info and push_info.get("server","") != server_id else "var(--text_muted)"
    vals = f'{{"repo_name":"{UI.escape(rn)}","local_path":"{UI.escape(lp)}"}}'
    return HTMLResponse(f'<div style="font-size:0.75rem;border:1px solid #ffaa44;border-radius:var(--radius);padding:0.6rem;font-family:monospace;"><div style="color:#ffaa44;font-weight:600;margin-bottom:0.4rem;">&#x26A0; Confirm Pull - {UI.escape(rn)}</div><div><b>You are on:</b> {UI.escape(server_id)}</div><div style="color:{warn_color};margin:0.3rem 0;">{push_line}</div><div><b>Gitea HEAD:</b> {remote_info}</div><div style="margin:0.3rem 0;"><b>Local HEAD:</b> {UI.escape(local_hash)} {UI.escape((local_msg or "")[-60:])}</div><div style="color:var(--text_muted);font-size:0.68rem;margin-bottom:0.5rem;">Pull will overwrite local changes.</div><div style="display:flex;gap:0.4rem;"><button class="ui-btn" style="background:var(--accent);color:var(--bg);font-size:0.75rem;" hx-post="{_P}/gitea/pull" hx-vals=\'{vals}\' hx-target="#gitea-op-out">&#x2713; Confirm Pull</button><button class="ui-btn" style="font-size:0.75rem;" onclick="document.getElementById(\'gitea-op-out\').innerHTML=\'\'">Cancel</button></div></div>')

@router.post("/gitea/pull")
async def gitea_pull(request: Request, repo_name: str = Form(...), local_path: str = Form(...)):
    cfg = _gitea_cfg()
    if not cfg: return HTMLResponse('<span style="color:#ff5f5f;">Gitea not configured.</span>')
    repo = {"repo_name":repo_name,"local_path":local_path}
    cwd  = _repo_git_dir(repo)
    _git_run(["remote","set-url","origin",_git_remote_url(cfg,repo_name)], cwd)
    rc, out = _git_run(["pull","origin","main"], cwd)
    return HTMLResponse(f'<pre style="color:{"var(--accent)" if rc==0 else "#ff5f5f"};font-size:0.75rem;">{UI.escape(out)}</pre>')

@router.post("/gitea/push-github")
async def gitea_push_github(request: Request):
    form = await request.form()
    repo_name  = form.get("repo_name",""); local_path = form.get("local_path","")
    commit_msg = (form.get("gitea-commit-msg") or form.get("gitea_commit_msg") or "").strip()
    gh_cfg = _github_cfg()
    if not gh_cfg: return HTMLResponse('<span style="color:#ff5f5f;">GitHub not configured.</span>')
    repo = {"repo_name":repo_name,"local_path":local_path}; cwd = _repo_git_dir(repo); steps = []
    rc, _ = _git_run(["remote","get-url","github"], cwd)
    rc2, out2 = _git_run(["remote","add" if rc!=0 else "set-url","github",_gh_remote_url(gh_cfg,repo_name)], cwd)
    steps.append(f"remote: {'set' if rc2==0 else out2[:60]}")
    if commit_msg:
        _git_run(["add","-A"], cwd); rc, out = _git_run(["commit","-m",commit_msg], cwd)
        steps.append(f"commit: {out.splitlines()[0][:60]}" if rc==0 and out else ("nothing new" if "nothing to commit" in out else out[:80]))
    rc, out = _git_run(["push","github","main"], cwd)
    steps.append(f"push to GitHub: {'╬ô┬ú├┤ ok' if rc==0 else out[:120]}")
    return HTMLResponse(f'<pre style="color:{"var(--accent)" if rc==0 else "#ff5f5f"};font-size:0.75rem;">{UI.escape(chr(10).join(steps))}</pre>')

@router.post("/github/configure")
async def github_configure(request: Request):
    form  = await request.form()
    user  = form.get("gh_user","").strip(); token = form.get("gh_token","").strip()
    if not user or not token: return HTMLResponse('<span style="color:#ff5f5f;">User and token required.</span>')
    try: _upsert_ui_strings([("github_user",user),("github_token",token)])
    except Exception as e: return HTMLResponse(f'<span style="color:#ff5f5f;">{UI.escape(str(e))}</span>')
    return HTMLResponse(await _gitea_panel_html(request))

@router.post("/github/disconnect")
async def github_disconnect(request: Request):
    try: _delete_ui_strings(["github_user","github_token"])
    except: pass
    return HTMLResponse(await _gitea_panel_html(request))

@router.post("/deploy/signal")
async def deploy_signal(request: Request):
    form = await request.form()
    prod_url   = form.get("prod_server_url", form.get("prod-server-url","")).strip()
    commit_msg = (form.get("gitea-commit-msg") or form.get("gitea_commit_msg") or "").strip()
    if prod_url:
        try: _upsert_ui_strings([("prod_server_url",prod_url)])
        except: pass
    steps = []
    cfg = _gitea_cfg()
    if commit_msg and cfg:
        server_repo = next((r for r in _repo_map() if r["type"]=="server"), None)
        if server_repo:
            cwd = _repo_git_dir(server_repo)
            _git_run(["remote","set-url","origin",_git_remote_url(cfg,server_repo["repo_name"])], cwd)
            _git_run(["add","-A"], cwd)
            rc, out = _git_run(["commit","-m",commit_msg.strip()], cwd)
            if rc == 0 or "nothing to commit" in out:
                rc2, out2 = _git_run(["push","origin","main"], cwd)
                steps.append(f"push to gitea: {'╬ô┬ú├┤' if rc2==0 else out2}")
            else: steps.append(f"commit failed: {out}")
    if not prod_url: steps.append("No production server URL set."); return HTMLResponse(f'<pre style="color:var(--text_muted);font-size:0.75rem;">{UI.escape(chr(10).join(steps))}</pre>')
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{prod_url.rstrip('/')}{_P}/deploy/receive", data={"secret":os.getenv("DEPLOY_SECRET",""),"repo":"server"})
            steps.append(f"{'╬ô┬ú├┤ Production acknowledged' if r.status_code==200 else f'HTTP {r.status_code}'}: {r.text[:200]}")
    except Exception as e: steps.append(f"Signal failed: {UI.escape(str(e))}")
    return HTMLResponse(f'<pre style="font-size:0.75rem;">{UI.escape(chr(10).join(steps))}</pre>')

@router.post("/deploy/receive")
async def deploy_receive(request: Request, secret: str = Form(""), repo: str = Form("server")):
    expected = os.getenv("DEPLOY_SECRET","")
    if not expected: return HTMLResponse("Deploy endpoint disabled. Set DEPLOY_SECRET to enable.", status_code=403)
    if secret != expected: return HTMLResponse("Unauthorized.", status_code=401)
    cfg = _gitea_cfg()
    if not cfg: return HTMLResponse("Gitea not configured on this server.")
    target_repo = next((r for r in _repo_map() if (r["type"]=="server" if repo=="server" else r["repo_name"]==repo)), None)
    if not target_repo: return HTMLResponse(f"Unknown repo: {repo}")
    cwd = _repo_git_dir(target_repo)
    _git_run(["remote","set-url","origin",_git_remote_url(cfg,target_repo["repo_name"])], cwd)
    rc, out = _git_run(["pull","origin","main"], cwd)
    if rc != 0: return HTMLResponse(f"Pull failed:\n{out}")
    (CODE_EDITOR_ROOT / ".restart_sentinel").write_text("restart")
    return HTMLResponse(f"Pulled. Restarting...\n{out}")

