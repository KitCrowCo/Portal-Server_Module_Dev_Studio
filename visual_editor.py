# modules/dev_studio/visual_editor
import inspect
import json
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

UI = None
md_plus_transpiler = None

router = APIRouter()

# --- PROPERTY PARSER LOGIC ---
def get_component_schema(name: str):
    """Introspects UI class to build property forms dynamically."""
    if not hasattr(UI, name): return None
    func = getattr(UI, name)
    sig = inspect.signature(func)
    fields = []
    for param_name, param in sig.parameters.items():
        if param_name in ['self', 'cls']: continue
        # Determine input type based on defaults or names
        val = param.default if param.default is not inspect.Parameter.empty else ""
        field_type = "text"
        if isinstance(val, bool): field_type = "checkbox"
        elif "color" in param_name: field_type = "color"
        fields.append({"name": param_name, "type": field_type, "default": val})
    return fields

def canvas_to_md_plus(state_json: str) -> str:
    """Converts Visual Canvas JSON state into MD++ markup."""
    try:
        elements = json.loads(state_json)
        # Sort by Y position to maintain top-to-bottom reading flow
        elements.sort(key=lambda e: float(e.get('y', 0)))
        lines = []
        for el in elements:
            e_type = el['type']
            p = el.get('props', {})
            if e_type == 'button':
                lines.append(f"[[Action: {p.get('label', 'Click')} | {p.get('url', '#')} | {p.get('target', '#content')}]]")
            elif e_type == 'card':
                lines.append(f"((glass))\n### {p.get('title', 'Title')}\n{p.get('content', '...')}\n(())")
            elif e_type == 'status':
                lines.append(f"[[Status: {p.get('label', 'Status')} | {p.get('val', 'OK')} | {p.get('ok', 'true')}]]")
            elif e_type == 'row':
                lines.append("((row))")
            elif e_type == 'end_group':
                lines.append("(())")
        return "\n".join(lines)
    except Exception as e:
        return f"Error transpiling: {str(e)}"

# --- ROUTES ---

@router.get("/design/{module_name}")
async def visual_editor_root(module_name: str):
    palette = [m[0] for m in inspect.getmembers(UI, predicate=inspect.isfunction) if not m[0].startswith("_")]
    tabs_html = UI.tab(f"🎨 {module_name} Designer", f"design://{module_name}", active=True)
    return HTMLResponse(f"""<div class="visual-editor-root" style="display:grid; grid-template-columns: 200px 1fr 250px; height: 100%;">
        <aside class="glass" style="padding:1rem; border-right:1px solid var(--border);">
            <div style="font-size:0.7rem; color:var(--accent); margin-bottom:1rem;">COMPONENTS</div>
            {"".join([f'<div class="palette-item" draggable="true" ondragstart="drag(event)" data-type="{i}" style="padding:0.4rem; border:1px solid var(--border); margin-bottom:0.4rem; cursor:grab; font-size:0.8rem;">{i}</div>' for i in palette])}
        </aside>
        <main style="display:flex; flex-direction:column;">
            <div id="canvas-area" style="flex:1; position:relative; background:var(--bg_panel); overflow:hidden;" ondrop="drop(event)" ondragover="allowDrop(event)">
                <div id="visual-canvas" style="width:2000px; height:2000px; background-image: radial-gradient(var(--border) 1px, transparent 1px); background-size: 20px 20px;"></div>
            </div>
            <input type="hidden" id="canvas-state" name="state" value='[]'>
        </main>

        <aside id="property-pane" class="glass" style="padding:1rem; border-left:1px solid var(--border);">
            <div id="props-form">Select element</div>
        </aside>
    </div>""")

@router.get("/canvas")
async def get_canvas(): return '<div id="visual-canvas" ondrop="drop(event)" ondragover="allowDrop(event)" style="width:100%; height:100%; position:relative;"></div>'

@router.get("/props/{component}")
async def get_props(component: str):
    schema = get_component_schema(component)
    if not schema: return "No properties available."
    inputs = []
    for field in schema:
        inputs.append(f"""<div style="margin-bottom:0.8rem;">
                              <label style="display:block; font-size:0.75rem; margin-bottom:0.2rem;">{field['name']}</label>
                              <input type="{field['type']}" name="{field['name']}" value="{field['default']}"  oninput="updateElementProp('{field['name']}', this.value)" style="width:100%; background:var(--bg); border:1px solid var(--border); color:var(--text); padding:4px;">
                           </div>""")
    return "".join(inputs)

@router.post("/preview")
async def get_preview(state: str = Form(...)):
    """Translates visual JSON state into MD++ and renders via transpiler."""
    try:
        data = json.loads(state)
        md_output = ""
        for item in sorted(data, key=lambda x: float(x['y'])):
            p = item['props']
            if item['type'] == 'button': md_output += f"[[Action: {p.get('label','Btn')} | {p.get('url','#')} | {p.get('target','#content')}]]\n"
            elif item['type'] == 'card': md_output += f"((glass))\n### {p.get('title','Title')}\n{p.get('content','...')}\n(())\n"
        return f'<div style="padding:2rem; overflow-y:auto; height:100%;">{md_plus_transpiler(md_output)}</div>'
    except:
        return "Error rendering preview."
