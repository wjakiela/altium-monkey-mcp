# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP server: read-only access to Altium projects (.PrjPcb / .SchDoc / .PcbDoc) via altium-monkey.

Works headless on Linux, no Altium Designer needed.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

import altium_monkey as am
from mcp.server.mcpserver import MCPServer

MIL_TO_MM = 0.0254
mcp = MCPServer(
    "altium",
    instructions=(
        "Read-only access to Altium Designer projects without Altium (schematic netlist across "
        "hierarchical sheets, BOM, PCB placement/routing/layers/rules, SVG/PNG rendering). "
        "Start with list_projects or project_info. `project` may be a .PrjPcb (preferred), "
        ".SchDoc or .PcbDoc path. Coordinates are reported in mm (and mils)."
    ),
)

# ---------------------------------------------------------------- cache

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _stamp(p: Path) -> float:
    """Newest mtime of Altium files next to the project (edits to any sheet invalidate cache)."""
    files = [p] + [f for f in p.parent.glob("*") if f.suffix.lower() in (".schdoc", ".pcbdoc", ".prjpcb")]
    return max(f.stat().st_mtime for f in files if f.exists())


def _resolve(project: str) -> Path:
    p = Path(project).expanduser().resolve()
    if p.is_dir():
        found = sorted(p.glob("*.PrjPcb")) + sorted(p.glob("*.prjpcb"))
        if not found:
            raise ValueError(f"No .PrjPcb in directory {p}")
        p = found[0]
    if not p.exists():
        raise ValueError(f"File not found: {p}")
    return p


def _ctx(project: str) -> dict[str, Any]:
    p = _resolve(project)
    key = str(p)
    st = _stamp(p)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == st:
            return hit[1]
        ext = p.suffix.lower()
        ctx: dict[str, Any] = {"path": p}
        if ext == ".prjpcb":
            ctx["design"] = am.AltiumDesign.from_prjpcb(p)
        elif ext == ".schdoc":
            ctx["design"] = am.AltiumDesign.from_schdoc(p)
        elif ext == ".pcbdoc":
            ctx["design"] = None
            ctx["pcb"] = am.AltiumPcbDoc.from_file(p)
        else:
            raise ValueError("Expected .PrjPcb, .SchDoc or .PcbDoc")
        _cache[key] = (st, ctx)
        return ctx


def _design(ctx) -> am.AltiumDesign:
    d = ctx.get("design")
    if d is None:
        raise ValueError("This tool needs a schematic (.PrjPcb or .SchDoc), not a bare .PcbDoc")
    return d


def _netlist(ctx):
    if "netlist" not in ctx:
        ctx["netlist"] = _design(ctx).to_netlist()
    return ctx["netlist"]


def _pcb(ctx) -> am.AltiumPcbDoc:
    if "pcb" not in ctx:
        d = _design(ctx)
        if not d.get_pcbdoc_paths():
            raise ValueError("Project has no .PcbDoc")
        ctx["pcb"] = d.load_pcbdoc()
    return ctx["pcb"]


def _mm(mils: float) -> float:
    return round(mils * MIL_TO_MM, 4)


def _layer_name(pcb, layer) -> str:
    try:
        return pcb.board.layer_name(int(layer))
    except Exception:
        return str(layer)


def _rx(pattern: str) -> re.Pattern:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(pattern), re.IGNORECASE)


def _params(params: dict | None, full: bool) -> dict:
    if not params:
        return {}
    if full:
        return {k: v for k, v in params.items() if v not in ("", None)}
    keep = ("Manufacturer", "Manufacturer Part Number", "MPN", "Value", "Comment", "Tolerance",
            "Voltage", "Power", "Supplier 1", "Supplier Part Number 1", "ComponentLink1URL")
    return {k: params[k] for k in keep if params.get(k) not in ("", None)}


def _comp_brief(c) -> dict:
    p = c.parameters or {}
    return {
        "designator": c.designator,
        "value": c.value,
        "comment": p.get("Comment"),
        "mpn": p.get("Manufacturer Part Number") or p.get("MPN"),
        "footprint": c.footprint,
        "description": c.description,
        "sheet": getattr(c.source_page, "source_sheet_file", None),
    }


def _pcb_comp(pcb, designator: str):
    for i, c in enumerate(pcb.components):
        if (c.designator or "").lower() == designator.lower():
            return i, c
    return None, None


# ---------------------------------------------------------------- tools


@mcp.tool()
def list_projects(root: str, max_depth: int = 6) -> list[dict]:
    """Find Altium projects (.PrjPcb) under a directory, with their sheets and PCB files."""
    rootp = Path(root).expanduser().resolve()
    out = []
    base_depth = len(rootp.parts)
    for dirpath, dirnames, filenames in os.walk(rootp):
        if len(Path(dirpath).parts) - base_depth >= max_depth:
            dirnames[:] = []
        # Skip Altium's backup/log folders and other noise.
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("History", "node_modules", "__Previews")
                       and not d.startswith("Project Logs for ") and not d.startswith("Project Outputs for ")]
        for f in filenames:
            if f.lower().endswith(".prjpcb"):
                d = Path(dirpath)
                out.append({
                    "project": str(d / f),
                    "schdocs": sorted(x for x in filenames if x.lower().endswith(".schdoc")),
                    "pcbdocs": sorted(x for x in filenames if x.lower().endswith(".pcbdoc")),
                })
    return sorted(out, key=lambda x: x["project"])


@mcp.tool()
def project_info(project: str) -> dict:
    """Overview: sheet hierarchy, variants, PCB docs, project parameters, component/net counts."""
    ctx = _ctx(project)
    if ctx.get("design") is None:
        return pcb_info(project)
    d = _design(ctx)
    cd = d.compile()
    nl = _netlist(ctx)
    sheets = [{
        "file": Path(str(pd.logical_document_id).split(":")[-1]).name,
        "sheet_number": pd.sheet_number,
        "parent": pd.parent_id.split(":")[-1] if pd.parent_id else None,
        "components": len(pd.component_ids or []),
    } for pd in cd.physical_documents]
    return {
        "path": str(ctx["path"]),
        "variants": d.get_variants(),
        "pcbdocs": [str(x) for x in d.get_pcbdoc_paths()],
        "project_parameters": d.get_pcb_project_parameters() if ctx["path"].suffix.lower() == ".prjpcb" else {},
        "sheets": sheets,
        "component_count": len(nl.components),
        "net_count": len(nl.nets),
        "compile_diagnostics": [str(x) for x in cd.diagnostics][:30],
    }


@mcp.tool()
def search_components(project: str, pattern: str, limit: int = 60) -> list[dict]:
    """Regex search (case-insensitive) over designator, value, comment, footprint, description and parameter values."""
    nl = _netlist(_ctx(project))
    rx = _rx(pattern)
    out = []
    for c in nl.components:
        hay = [c.designator, c.value, c.footprint, c.description, c.library_ref, *(c.parameters or {}).values()]
        if any(h and rx.search(str(h)) for h in hay):
            out.append(_comp_brief(c))
            if len(out) >= limit:
                break
    return out


@mcp.tool()
def get_component(project: str, designator: str, all_parameters: bool = False) -> dict:
    """Component details: parameters, every pin with its net (and other parts on that net), and PCB placement."""
    ctx = _ctx(project)
    res: dict[str, Any] = {}
    if ctx.get("design") is not None:
        nl = _netlist(ctx)
        c = nl.get_component(designator)
        if c is None:
            raise ValueError(f"Component {designator} not found")
        res.update(_comp_brief(c))
        res["library_ref"] = c.library_ref
        res["exclude_from_bom"] = c.exclude_from_bom
        res["parameters"] = _params(c.parameters, all_parameters)
        pins = []
        for net in nl.get_nets_for_component(c.designator):
            mine = [t for t in net.terminals if t.designator == c.designator]
            others = sorted({f"{t.designator}.{t.pin}" for t in net.terminals if t.designator != c.designator})
            for t in mine:
                pins.append({"pin": t.pin, "pin_name": t.pin_name, "type": t.pin_type.name,
                             "net": net.name, "connected_to": others[:25] + (["..."] if len(others) > 25 else [])})
        pins.sort(key=lambda x: (len(x["pin"]), x["pin"]))
        res["pins"] = pins
    try:
        pcb = _pcb(ctx)
        idx, pc = _pcb_comp(pcb, designator)
        if pc is not None:
            x, y = pcb.get_component_pnp_position_mils(idx)
            res["pcb"] = {"footprint": pc.footprint, "layer": pc.get_layer_normalized(),
                          "rotation_deg": pc.get_rotation_degrees(),
                          "center_mm": [_mm(x), _mm(y)], "center_mils": [round(x, 2), round(y, 2)]}
            res.setdefault("designator", pc.designator)
    except ValueError:
        pass
    return res


@mcp.tool()
def search_nets(project: str, pattern: str, limit: int = 200) -> list[dict]:
    """Regex search over net names; returns name and pin count."""
    nl = _netlist(_ctx(project))
    rx = _rx(pattern)
    out = [{"net": n.name, "pins": len(n.terminals)} for n in nl.nets if n.name and rx.search(n.name)]
    return sorted(out, key=lambda x: x["net"])[:limit]


@mcp.tool()
def get_net(project: str, net: str, include_routing: bool = True) -> dict:
    """All pins on a net (designator, pin number/name, type, part value) and, if a PCB exists,
    routing stats: track length/width per layer, vias, pads."""
    ctx = _ctx(project)
    res: dict[str, Any] = {"net": net}
    if ctx.get("design") is not None:
        nl = _netlist(ctx)
        n = nl.get_net(net) or next((x for x in nl.nets if (x.name or "").lower() == net.lower()), None)
        if n is None:
            raise ValueError(f"Net {net} not found (try search_nets)")
        res["net"] = n.name
        comps = {c.designator: c for c in nl.components}
        res["sheets"] = sorted({sp.source_sheet_file for sp in (n.source_pages or [])})
        res["pins"] = [{
            "pin": f"{t.designator}.{t.pin}", "pin_name": t.pin_name, "type": t.pin_type.name,
            "part": (comps[t.designator].parameters or {}).get("Comment") or comps[t.designator].value
            if t.designator in comps else None,
        } for t in sorted(n.terminals, key=lambda t: (t.designator, t.pin))]
    if include_routing:
        try:
            pcb = _pcb(ctx)
        except ValueError:
            return res
        idx = next((i for i, x in enumerate(pcb.nets) if (x.name or "").lower() == res["net"].lower()), None)
        if idx is None:
            res["routing"] = "net not present on PCB"
            return res
        prim = pcb.get_net_primitives(idx)
        layers: dict[str, dict] = {}
        for t in prim.get("tracks", []):
            ln = _layer_name(pcb, t.layer)
            L = layers.setdefault(ln, {"tracks": 0, "length_mm": 0.0, "widths_mm": set()})
            L["tracks"] += 1
            L["length_mm"] += math.hypot(t.end_x_mils - t.start_x_mils, t.end_y_mils - t.start_y_mils) * MIL_TO_MM
            L["widths_mm"].add(_mm(t.width_mils))
        for L in layers.values():
            L["length_mm"] = round(L["length_mm"], 2)
            L["widths_mm"] = sorted(L["widths_mm"])
        vias = prim.get("vias", [])
        res["routing"] = {
            "layers": layers,
            "total_track_length_mm": round(sum(L["length_mm"] for L in layers.values()), 2),
            "vias": len(vias),
            "via_sizes_mm": sorted({(_mm(v.diameter_mils), _mm(v.hole_size_mils)) for v in vias}),
            "pads": len(prim.get("pads", [])),
            "arcs": len(prim.get("arcs", [])),
            "polygons": len(prim.get("polygons", [])) + len(prim.get("regions", [])) + len(prim.get("fills", [])),
        }
    return res


@mcp.tool()
def get_bom(project: str, variant: str | None = None, grouped: bool = True, pattern: str | None = None) -> list[dict]:
    """Bill of materials (optionally for a design variant). grouped=True merges identical parts."""
    d = _design(_ctx(project))
    rows = d.to_bom(variant)
    rx = _rx(pattern) if pattern else None
    items = []
    for r in rows:
        p = r.get("parameters") or {}
        it = {"designator": r["designator"], "comment": p.get("Comment") or r.get("value"),
              "mpn": p.get("Manufacturer Part Number") or p.get("MPN"), "manufacturer": p.get("Manufacturer"),
              "footprint": r.get("footprint"), "description": r.get("description")}
        if rx and not any(v and rx.search(str(v)) for v in it.values()):
            continue
        items.append(it)
    if not grouped:
        return items
    groups: dict[tuple, dict] = {}
    for it in items:
        k = (it["comment"], it["mpn"], it["footprint"])
        g = groups.setdefault(k, {**it, "designators": [], "qty": 0})
        g["designators"].append(it["designator"])
        g["qty"] += 1
    out = []
    for g in groups.values():
        g.pop("designator", None)
        g["designators"] = sorted(g["designators"], key=lambda s: (re.sub(r"\d+", "", s), int(re.sub(r"\D", "", s) or 0)))
        out.append(g)
    return sorted(out, key=lambda g: g["designators"][0])


_RULE_BOILERPLATE = {
    "SELECTION", "LAYER", "LOCKED", "POLYGONOUTLINE", "USERROUTED", "KEEPOUT", "UNIONINDEX", "RULEKIND",
    "NETSCOPE", "LAYERKIND", "SCOPE1EXPRESSION", "SCOPE2EXPRESSION", "NAME", "ENABLED", "PRIORITY",
    "UNIQUEID", "DEFINEDBYLOGICALDOCUMENT", "COMMENT",
}


@mcp.tool()
def pcb_info(project: str, include_rules: bool = True) -> dict:
    """PCB overview: board size, copper layers in use with stackup, primitive counts,
    net classes, differential pairs and design rules."""
    pcb = _pcb(_ctx(project))
    b = pcb.board
    xs = [v.x_mils for v in b.outline.vertices] if b.outline else []
    ys = [v.y_mils for v in b.outline.vertices] if b.outline else []
    used: dict[str, int] = {}
    for t in pcb.tracks:
        ln = _layer_name(pcb, t.layer)
        used[ln] = used.get(ln, 0) + 1
    stack = [str(s) for s in (b.v9_stack or [])]
    res: dict[str, Any] = {
        "path": str(pcb.filepath),
        "board_size_mm": [_mm(max(xs) - min(xs)), _mm(max(ys) - min(ys))] if xs else None,
        "tracks_per_layer": dict(sorted(used.items(), key=lambda kv: -kv[1])),
        "stackup": stack,
        "counts": {"components": len(pcb.components), "nets": len(pcb.nets), "tracks": len(pcb.tracks),
                   "arcs": len(pcb.arcs), "vias": len(pcb.vias), "pads": len(pcb.pads),
                   "polygons": len(pcb.polygons)},
        "via_sizes_mm": sorted({(_mm(v.diameter_mils), _mm(v.hole_size_mils)) for v in pcb.vias}),
        "net_classes": {nc.name: list(getattr(nc, "members", []) or []) for nc in pcb.net_classes
                        if getattr(nc, "members", None)},
        "differential_pairs": [getattr(dp, "name", str(dp)) for dp in pcb.differential_pairs],
    }
    if include_rules:
        res["rules"] = [{
            "name": r.name, "kind": r.rule_kind, "enabled": r.enabled, "priority": r.priority,
            "net_scope": r.net_scope, "scope1": r.scope1_expression, "scope2": r.scope2_expression,
            "values": {k: v[:200] for k, v in (r.raw_record or {}).items()
                       if k not in _RULE_BOILERPLATE and v not in ("", None)},
        } for r in pcb.rules]
    return res


@mcp.tool()
def pcb_placement(project: str, pattern: str = ".*", variant: str | None = None, units: str = "mm") -> list[dict]:
    """Pick-and-place: designator, footprint, layer (top/bottom), center X/Y and rotation.
    `pattern` filters by designator/comment/footprint regex."""
    ctx = _ctx(project)
    rx = _rx(pattern)
    if ctx.get("design") is not None:
        rows = _design(ctx).to_pnp(variant, units=units)
        out = [{"designator": r.designator, "comment": r.comment, "footprint": r.footprint, "layer": r.layer,
                "x": float(r.center_x), "y": float(r.center_y), "rotation": float(r.rotation)} for r in rows]
    else:
        pcb = _pcb(ctx)
        k = MIL_TO_MM if units == "mm" else 1.0
        out = []
        for i, c in enumerate(pcb.components):
            x, y = pcb.get_component_pnp_position_mils(i)
            out.append({"designator": c.designator, "comment": None, "footprint": c.footprint,
                        "layer": c.get_layer_normalized(), "x": round(x * k, 4), "y": round(y * k, 4),
                        "rotation": c.get_rotation_degrees()})
    return [r for r in out if any(v and rx.search(str(v)) for v in (r["designator"], r["comment"], r["footprint"]))]


@mcp.tool()
def components_near(project: str, designator: str, radius_mm: float = 5.0) -> list[dict]:
    """PCB components whose centers lie within radius_mm of the given component (same and opposite side)."""
    pcb = _pcb(_ctx(project))
    idx, ref = _pcb_comp(pcb, designator)
    if ref is None:
        raise ValueError(f"{designator} not on PCB")
    x0, y0 = pcb.get_component_pnp_position_mils(idx)
    out = []
    for i, c in enumerate(pcb.components):
        if i == idx:
            continue
        x, y = pcb.get_component_pnp_position_mils(i)
        dist = math.hypot(x - x0, y - y0) * MIL_TO_MM
        if dist <= radius_mm:
            out.append({"designator": c.designator, "footprint": c.footprint, "layer": c.get_layer_normalized(),
                        "distance_mm": round(dist, 2)})
    return sorted(out, key=lambda r: r["distance_mm"])


def _write_render(svg: str, out_dir: str | None, stem: str, png: bool, png_width: int, crop: bool = False) -> dict:
    od = Path(out_dir).expanduser() if out_dir else Path(tempfile.gettempdir()) / "altium-mcp-renders"
    od.mkdir(parents=True, exist_ok=True)
    svg_path = od / f"{stem}.svg"
    svg_path.write_text(svg, encoding="utf-8")
    res = {"svg": str(svg_path)}
    if png:
        inkscape = shutil.which("inkscape")
        png_path = od / f"{stem}.png"
        if inkscape:
            r = subprocess.run([inkscape, str(svg_path), "--export-type=png", f"--export-filename={png_path}",
                                f"--export-width={png_width}", "--export-background=white",
                                "--export-background-opacity=1", *(["--export-area-drawing"] if crop else [])],
                               capture_output=True, text=True, timeout=300)
            if png_path.exists():
                res["png"] = str(png_path)
            else:
                res["png_error"] = (r.stderr or r.stdout)[-500:]
        else:
            res["png_error"] = "inkscape not found"
    return res


@mcp.tool()
def render_schematic(project: str, sheet: str, out_dir: str | None = None, png: bool = True, png_width: int = 3000) -> dict:
    """Render one schematic sheet (file name, e.g. 'sterowanie.SchDoc') to SVG and PNG. Returns file paths;
    open the PNG with the Read tool to look at it."""
    ctx = _ctx(project)
    d = _design(ctx)
    cd = d.compile()
    doc = next((pd for pd in cd.physical_documents
                if Path(str(pd.logical_document_id).split(":")[-1]).name.lower() == Path(sheet).name.lower()), None)
    if doc is None:
        names = [Path(str(pd.logical_document_id).split(":")[-1]).name for pd in cd.physical_documents]
        raise ValueError(f"Sheet {sheet} not found; available: {names}")
    svg = d.to_physical_svg(doc.id)
    return _write_render(svg, out_dir, f"{ctx['path'].stem}_{Path(sheet).stem}", png, png_width)


@mcp.tool()
def render_pcb(project: str, layers: list[str] | None = None, out_dir: str | None = None,
               png: bool = True, png_width: int = 3000, pad_designators: bool = False) -> dict:
    """Render the PCB to SVG/PNG. `layers` = layer names to show (e.g. ['TOP','TOP_OVERLAY','MULTI_LAYER'],
    'TOPOVERLAY' style also accepted); None renders all layers. Returns file paths; open the PNG with the Read tool."""
    ctx = _ctx(project)
    pcb = _pcb(ctx)
    opts = am.PcbSvgRenderOptions()
    opts.show_pad_designators = pad_designators
    opts.clip_all_layers_to_board_outline = True   # stray objects far off-board would blow up the canvas
    opts.show_origin_datum = False
    if layers:
        by_key = {l.name.replace("_", ""): l for l in am.PcbLayer}
        wanted = []
        for ln in layers:
            lay = by_key.get(ln.upper().replace("_", "").replace(" ", ""))
            if lay is None:
                raise ValueError(f"Unknown layer {ln}; valid: {[l.name for l in am.PcbLayer]}")
            wanted.append(lay)
        opts.visible_layers = set(wanted)
    svg = pcb.to_svg(opts)
    tag = "_".join(l.lower() for l in layers) if layers else "all"
    return _write_render(svg, out_dir, f"{ctx['path'].stem}_pcb_{tag}", png, png_width, crop=True)


@mcp.tool()
def export_netlist_json(project: str, out_path: str) -> dict:
    """Write the full compiled netlist (components, nets, terminals, hierarchy) as JSON to out_path."""
    nl = _netlist(_ctx(project))
    p = Path(out_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(nl.to_json_text(), encoding="utf-8")
    return {"written": str(p), "bytes": p.stat().st_size}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
