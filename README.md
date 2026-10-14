# altium-monkey-mcp

Read-only [MCP](https://modelcontextprotocol.io/) server that lets AI assistants (Claude Code, Claude Desktop, VS Code, ...)
inspect Altium Designer projects — schematics, netlist, BOM and PCB — on Linux, macOS or Windows,
**without Altium Designer installed**.

It is a thin wrapper around [altium-monkey](https://github.com/wavenumber-eng/altium_monkey), which parses the
binary `.SchDoc` / `.PcbDoc` / `.PrjPcb` formats directly.

## Tools

| Tool | Description |
|---|---|
| `list_projects` | Find `.PrjPcb` projects under a directory |
| `project_info` | Sheet hierarchy, variants, project parameters, counts, compile diagnostics |
| `search_components` | Regex search over designator, value, footprint, description, parameters |
| `get_component` | Parameters, every pin with its net and what else is on that net, PCB placement |
| `search_nets` | Regex search over net names |
| `get_net` | All pins on a net + PCB routing stats (track length/width per layer, vias) |
| `get_bom` | Bill of materials, optionally per design variant, grouped |
| `pcb_info` | Board size, layer stackup, net classes, differential pairs, design rules |
| `pcb_placement` | Pick-and-place (X/Y, rotation, side) |
| `components_near` | Components within a radius of a given part |
| `render_schematic` | Render a sheet to SVG + PNG |
| `render_pcb` | Render selected PCB layers to SVG + PNG (cropped to the board) |
| `export_netlist_json` | Write the full compiled netlist to a JSON file |

`project` accepts a `.PrjPcb` (recommended), `.SchDoc`, `.PcbDoc`, or a directory containing a project.
Parsed designs are cached in memory and reloaded when any design file changes.
Coordinates are reported in mm.

## Requirements

- Python 3.12–3.14 (required by altium-monkey)
- [Inkscape](https://inkscape.org/) on `PATH` — optional, only for PNG output of the render tools

## Installation

```bash
git clone https://github.com/wjakiela/altium-monkey-mcp.git
cd altium-monkey-mcp
uv venv --python 3.12
uv pip install -e .
```

### Claude Code (available in all projects)

```bash
claude mcp add --scope user altium -- "$PWD/.venv/bin/altium-monkey-mcp"
```

### Claude Desktop / other clients

```json
{
  "mcpServers": {
    "altium": {
      "command": "/absolute/path/to/altium-monkey-mcp/.venv/bin/altium-monkey-mcp"
    }
  }
}
```

## Example prompts

- "Which ESP32 pins are connected to the 32 kHz crystal, and what load capacitors are used?"
- "List all parts on net V_BAT and how long the trace is on each layer."
- "Render the bottom layer of the PCB and show me where Y1 sits."
- "What clearance and width rules does the board use?"

## Limitations

- Read-only: the server never modifies design files.
- Accuracy depends on altium-monkey; Linux support there is less tested than Windows.
- Arc length is not included in routing length; polygons/planes are counted, not measured.

## License

AGPL-3.0-or-later, because the project depends on altium-monkey (AGPL-3.0-or-later).
Altium and Altium Designer are trademarks of Altium Limited; this project is not affiliated with Altium.
