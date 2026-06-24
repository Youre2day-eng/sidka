"""Design skill — provides a dark-theme SVG/HTML design spec for the agent to use when generating mockups."""

NAME = "design"
DESCRIPTION = (
    "Returns a dark-theme design system spec (colors, typography, spacing, SVG constraints) for DJ's RunAI cockpit. "
    "Call this before generating an SVG mockup, diagram, or chart so the output matches the RunAI aesthetic. "
    "Actions: spec (full spec for a given type), colors (palette only), sizes (spacing/typography scale)."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["spec", "colors", "sizes"],
            "description": "spec = full design brief; colors = palette only; sizes = spacing/type scale",
        },
        "type": {
            "type": "string",
            "description": "mockup | diagram | chart | flowchart | icon | wireframe",
        },
        "description": {
            "type": "string",
            "description": "What the graphic is for — used to tailor the spec",
        },
        "width": {"type": "integer", "description": "Target SVG width in px (default 680)"},
        "height": {"type": "integer", "description": "Target SVG height in px (default auto)"},
    },
    "required": [],
}
TOOLS = [{"type": "function", "function": {"name": NAME, "description": DESCRIPTION, "parameters": SCHEMA}}]

# ── Design tokens ──────────────────────────────────────────────────────────
COLORS = {
    "bg_base":     "#111111",
    "bg_surface":  "#1a1a1a",
    "bg_raised":   "#222222",
    "bg_hover":    "#2a2a2a",
    "border":      "#2e2e2e",
    "border_soft": "#1e1e1e",
    "text_primary":  "#e8e8e8",
    "text_secondary": "#a0a0a0",
    "text_muted":  "#555555",
    "accent":      "#7c6af7",   # purple
    "accent_dim":  "#3d3466",
    "green":       "#58a058",
    "green_dim":   "#1a3a1a",
    "amber":       "#d4a020",
    "amber_dim":   "#3a2c00",
    "red":         "#e05050",
    "red_dim":     "#3a1010",
    "blue":        "#5080d0",
    "blue_dim":    "#101830",
}

SIZES = {
    "font_base": 13,
    "font_sm": 11,
    "font_xs": 10,
    "font_lg": 15,
    "font_h1": 18,
    "font_h2": 15,
    "radius_sm": 4,
    "radius_md": 8,
    "radius_lg": 12,
    "padding_sm": 8,
    "padding_md": 12,
    "padding_lg": 16,
    "gap_sm": 6,
    "gap_md": 10,
    "gap_lg": 16,
}

SVG_RULES = [
    "Output a single fenced ```svg code block — no prose before or after",
    "viewBox starts at '0 0 {width} {height}', width attribute matches",
    "Background rect fills the entire viewBox with bg_base color",
    "All text uses font-family='monospace' or 'system-ui, sans-serif'",
    "No external images, fonts, or URLs — fully self-contained",
    "Remove any <script> tags — pure declarative SVG only",
    "Use <defs> + <clipPath> for overflow clipping on components",
    "Labels: font-size matches the sizes scale; fill matches text colors",
    "Interactive states (hover, selected) use the *_dim background variants",
    "Prefer rounded rects (rx=4 for components, rx=8 for panels, rx=12 for cards)",
    "Show realistic sample data — not 'Lorem ipsum' or 'Item 1, Item 2'",
]

HTML_RULES = [
    "Output a single fenced ```html code block — no prose before or after",
    "Fully self-contained: all CSS inline or in a <style> block, no external deps",
    "Background: bg_base (#111111)",
    "Font: system-ui, -apple-system, sans-serif at 13px",
    "Use the design tokens for all colors (hardcode hex values inline)",
    "Interactive elements should work without JS frameworks",
    "Target width: 680px or 100%",
]

TYPE_GUIDANCE = {
    "mockup": (
        "Show a realistic UI mockup with actual UI components (buttons, inputs, cards, sidebars). "
        "Use the raised surface colors for panels. Include at least one interactive-looking element."
    ),
    "diagram": (
        "Architecture or flow diagram. Use boxes for components, arrows for data flow. "
        "Color-code component types (accent for active/primary, green for output, blue for data stores). "
        "Add short descriptive labels on connections."
    ),
    "chart": (
        "Data visualization. Include axis labels, a title, and at least 6 data points. "
        "Use the accent color for primary series, muted text for axes. "
        "Add value labels on bars or data points."
    ),
    "flowchart": (
        "Decision flowchart. Diamond shapes for decisions, rounded rects for steps, "
        "circles for start/end. Yes/No labels on branches. Clear flow direction (top to bottom)."
    ),
    "wireframe": (
        "Low-fidelity wireframe using only border_soft outlines and muted text. "
        "Placeholder blocks for images. Focus on layout and hierarchy, not color."
    ),
    "icon": (
        "SVG icon or set of icons. Clean, single-weight strokes at 2px. "
        "Use text_secondary color. Center in a 40x40 or 24x24 cell."
    ),
}


def run(args):
    action = (args.get("action") or "spec").lower()
    kind = (args.get("type") or "mockup").lower()
    description = args.get("description") or ""
    width = args.get("width") or 680
    height = args.get("height") or "auto"

    if action == "colors":
        lines = ["RunAI Dark Palette:"]
        for k, v in COLORS.items():
            lines.append(f"  {k:20s}  {v}")
        return "\n".join(lines)

    if action == "sizes":
        lines = ["RunAI Spacing & Typography:"]
        for k, v in SIZES.items():
            lines.append(f"  {k:20s}  {v}")
        return "\n".join(lines)

    # action == "spec"
    type_note = TYPE_GUIDANCE.get(kind, TYPE_GUIDANCE["mockup"])
    rules = SVG_RULES if kind != "html" else HTML_RULES
    c = COLORS

    spec = f"""=== RunAI Design Spec — {kind.upper()} ===

Target: {description or 'UI graphic'} ({width}x{height if height != 'auto' else 'flexible'} px)

TYPE GUIDANCE
{type_note}

KEY COLORS (use these exact hex values)
  Background base:    {c['bg_base']}
  Surface / panel:    {c['bg_surface']}
  Raised card:        {c['bg_raised']}
  Border:             {c['border']}
  Text primary:       {c['text_primary']}
  Text secondary:     {c['text_secondary']}
  Text muted:         {c['text_muted']}
  Accent (purple):    {c['accent']}  (dim: {c['accent_dim']})
  Green / success:    {c['green']}  (dim: {c['green_dim']})
  Amber / warning:    {c['amber']}  (dim: {c['amber_dim']})
  Red / error:        {c['red']}  (dim: {c['red_dim']})
  Blue / info:        {c['blue']}  (dim: {c['blue_dim']})

TYPOGRAPHY
  Body: 13px  |  Small: 11px  |  Micro: 10px
  H1: 18px/500  |  H2: 15px/500  |  Label: 10px/600 uppercase

SPACING
  Radius: 4 (element) / 8 (panel) / 12 (card)
  Padding: 8 (sm) / 12 (md) / 16 (lg)
  Gap: 6 / 10 / 16

OUTPUT RULES
""" + "\n".join(f"  {i+1}. {r}" for i, r in enumerate(rules))

    return spec
