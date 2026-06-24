"""Chart skill — generates dark-theme SVG charts from data. Returns SVG markup the agent pastes in a ```svg block."""

NAME = "chart"
DESCRIPTION = (
    "Generates a self-contained dark-theme SVG chart from data. "
    "Returns complete SVG markup — the agent pastes it inside a ```svg code block so it renders inline. "
    "Types: bar (vertical bars), hbar (horizontal bars), line (with points), pie, donut, sparkline (mini inline)."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "type":   {"type": "string", "enum": ["bar", "hbar", "line", "pie", "donut", "sparkline"],
                   "description": "Chart type"},
        "data":   {"type": "array",
                   "items": {"type": "object", "properties": {
                       "label": {"type": "string"}, "value": {"type": "number"}
                   }},
                   "description": "Array of {label, value} objects"},
        "title":  {"type": "string",  "description": "Chart title"},
        "width":  {"type": "integer", "description": "SVG width in px (default 600)"},
        "height": {"type": "integer", "description": "SVG height in px (default 320)"},
        "color":  {"type": "string",  "description": "Override accent color hex (default #7c6af7)"},
    },
    "required": ["type", "data"],
}
TOOLS = [{"type": "function", "function": {"name": NAME, "description": DESCRIPTION, "parameters": SCHEMA}}]

# ── dark theme tokens ──────────────────────────────────────────────────────
BG      = "#111111"
SURFACE = "#1a1a1a"
BORDER  = "#2e2e2e"
TEXT    = "#e8e8e8"
MUTED   = "#666666"
GRID    = "#222222"

PALETTE = ["#7c6af7", "#58a058", "#d4a020", "#e05050", "#5080d0",
           "#c070e0", "#40a0b0", "#d08040", "#80b060", "#e07090"]


def run(args):
    kind   = (args.get("type") or "bar").lower()
    data   = args.get("data") or []
    title  = args.get("title") or ""
    width  = int(args.get("width")  or 600)
    height = int(args.get("height") or (200 if kind == "sparkline" else 320))
    accent = args.get("color") or PALETTE[0]

    if not data:
        return "No data provided."

    if kind == "bar":    return _bar(data, title, width, height, accent, horizontal=False)
    if kind == "hbar":   return _bar(data, title, width, height, accent, horizontal=True)
    if kind == "line":   return _line(data, title, width, height, accent)
    if kind == "pie":    return _pie(data, title, width, height, False)
    if kind == "donut":  return _pie(data, title, width, height, True)
    if kind == "sparkline": return _sparkline(data, width, height, accent)
    return "Unknown chart type."


def _svg_open(w, h):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}"'
            f' style="font-family:system-ui,sans-serif;background:{BG}">')

def _svg_close():
    return '</svg>'

def _title_el(title, w, y=22):
    if not title:
        return ""
    return f'<text x="{w//2}" y="{y}" text-anchor="middle" font-size="14" font-weight="600" fill="{TEXT}">{_esc(title)}</text>'

def _esc(s):
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"','&quot;')

def _fmt_val(v):
    if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
    if v >= 1_000:     return f"{v/1_000:.1f}k"
    if isinstance(v, float): return f"{v:.1f}"
    return str(int(v))


def _bar(data, title, w, h, accent, horizontal=False):
    pad_top  = 40 if title else 20
    pad_bot  = 50
    pad_left = 55 if not horizontal else 110
    pad_right = 20

    values = [d.get("value", 0) for d in data]
    labels = [str(d.get("label", "")) for d in data]
    max_v  = max(values) if values else 1
    n      = len(data)

    parts = [_svg_open(w, h)]
    parts.append(f'<rect width="{w}" height="{h}" fill="{BG}"/>')
    if title:
        parts.append(_title_el(title, w))

    if not horizontal:
        chart_w = w - pad_left - pad_right
        chart_h = h - pad_top - pad_bot
        bar_w   = max(4, chart_w // n - 6)
        step    = chart_w / n

        # grid lines
        for i in range(5):
            y = pad_top + chart_h - (chart_h * i // 4)
            val = max_v * i / 4
            parts.append(f'<line x1="{pad_left}" y1="{y}" x2="{w-pad_right}" y2="{y}" stroke="{GRID}" stroke-width="1"/>')
            parts.append(f'<text x="{pad_left-6}" y="{y+4}" text-anchor="end" font-size="10" fill="{MUTED}">{_fmt_val(val)}</text>')

        for i, (label, value) in enumerate(zip(labels, values)):
            bh = max(2, int(chart_h * value / max_v))
            x  = pad_left + int(i * step + (step - bar_w) / 2)
            y  = pad_top + chart_h - bh
            col = PALETTE[i % len(PALETTE)] if accent == PALETTE[0] else accent
            parts.append(f'<rect x="{x}" y="{y}" width="{bar_w}" height="{bh}" rx="3" fill="{col}" opacity="0.9"/>')
            parts.append(f'<text x="{x + bar_w//2}" y="{y - 5}" text-anchor="middle" font-size="10" fill="{TEXT}">{_fmt_val(value)}</text>')
            lx = x + bar_w // 2
            ly = pad_top + chart_h + 16
            parts.append(f'<text x="{lx}" y="{ly}" text-anchor="middle" font-size="11" fill="{MUTED}">{_esc(label[:12])}</text>')
    else:
        chart_w = w - pad_left - pad_right
        chart_h = h - pad_top - 20
        bar_h   = max(4, chart_h // n - 6)
        step    = chart_h / n

        for i in range(5):
            x = pad_left + int(chart_w * i / 4)
            val = max_v * i / 4
            parts.append(f'<line x1="{x}" y1="{pad_top}" x2="{x}" y2="{pad_top+chart_h}" stroke="{GRID}" stroke-width="1"/>')
            parts.append(f'<text x="{x}" y="{pad_top+chart_h+14}" text-anchor="middle" font-size="10" fill="{MUTED}">{_fmt_val(val)}</text>')

        for i, (label, value) in enumerate(zip(labels, values)):
            bw  = max(2, int(chart_w * value / max_v))
            y   = pad_top + int(i * step + (step - bar_h) / 2)
            col = PALETTE[i % len(PALETTE)] if accent == PALETTE[0] else accent
            parts.append(f'<rect x="{pad_left}" y="{y}" width="{bw}" height="{bar_h}" rx="3" fill="{col}" opacity="0.9"/>')
            parts.append(f'<text x="{pad_left + bw + 6}" y="{y + bar_h//2 + 4}" font-size="11" fill="{TEXT}">{_fmt_val(value)}</text>')
            parts.append(f'<text x="{pad_left - 6}" y="{y + bar_h//2 + 4}" text-anchor="end" font-size="11" fill="{MUTED}">{_esc(label[:16])}</text>')

    parts.append(_svg_close())
    return "\n".join(parts)


def _line(data, title, w, h, accent):
    pad_top   = 40 if title else 20
    pad_bot   = 50
    pad_left  = 55
    pad_right = 20

    values = [d.get("value", 0) for d in data]
    labels = [str(d.get("label", "")) for d in data]
    n      = len(data)
    max_v  = max(values) if values else 1
    min_v  = min(values) if values else 0
    rng    = max_v - min_v or 1

    chart_w = w - pad_left - pad_right
    chart_h = h - pad_top - pad_bot

    def px(i, v):
        x = pad_left + int(i * chart_w / (n - 1)) if n > 1 else pad_left + chart_w // 2
        y = pad_top + chart_h - int((v - min_v) / rng * chart_h)
        return x, y

    parts = [_svg_open(w, h)]
    parts.append(f'<rect width="{w}" height="{h}" fill="{BG}"/>')
    if title:
        parts.append(_title_el(title, w))

    # grid
    for i in range(5):
        y   = pad_top + chart_h - (chart_h * i // 4)
        val = min_v + rng * i / 4
        parts.append(f'<line x1="{pad_left}" y1="{y}" x2="{w-pad_right}" y2="{y}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{pad_left-6}" y="{y+4}" text-anchor="end" font-size="10" fill="{MUTED}">{_fmt_val(val)}</text>')

    # area fill
    if n > 1:
        pts = [px(i, v) for i, v in enumerate(values)]
        path = f"M{pts[0][0]},{pts[0][1]}"
        for x, y in pts[1:]:
            path += f" L{x},{y}"
        area = path + f" L{pts[-1][0]},{pad_top+chart_h} L{pts[0][0]},{pad_top+chart_h} Z"
        parts.append(f'<path d="{area}" fill="{accent}" opacity="0.12"/>')
        parts.append(f'<path d="{path}" fill="none" stroke="{accent}" stroke-width="2" stroke-linejoin="round"/>')

    # dots + labels
    for i, (label, value) in enumerate(zip(labels, values)):
        x, y = px(i, value)
        parts.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{accent}" stroke="{BG}" stroke-width="2"/>')
        # x-axis label (every other if many points)
        if n <= 12 or i % 2 == 0:
            parts.append(f'<text x="{x}" y="{pad_top+chart_h+16}" text-anchor="middle" font-size="10" fill="{MUTED}">{_esc(label[:10])}</text>')

    parts.append(_svg_close())
    return "\n".join(parts)


def _pie(data, title, w, h, donut=False):
    import math
    cx = w // 2
    cy = h // 2 + (10 if title else 0)
    r  = min(cx, cy) - 30

    values = [max(0, d.get("value", 0)) for d in data]
    labels = [str(d.get("label", "")) for d in data]
    total  = sum(values) or 1

    parts = [_svg_open(w, h)]
    parts.append(f'<rect width="{w}" height="{h}" fill="{BG}"/>')
    if title:
        parts.append(_title_el(title, w, 22))

    angle = -math.pi / 2
    for i, (label, value) in enumerate(zip(labels, values)):
        sweep = 2 * math.pi * value / total
        x1 = cx + r * math.cos(angle)
        y1 = cy + r * math.sin(angle)
        x2 = cx + r * math.cos(angle + sweep)
        y2 = cy + r * math.sin(angle + sweep)
        large = 1 if sweep > math.pi else 0
        col   = PALETTE[i % len(PALETTE)]
        path  = f"M{cx},{cy} L{x1:.1f},{y1:.1f} A{r},{r} 0 {large},1 {x2:.1f},{y2:.1f} Z"
        parts.append(f'<path d="{path}" fill="{col}" stroke="{BG}" stroke-width="2"/>')

        # label at midpoint angle
        mid_a = angle + sweep / 2
        lr    = r * 0.7 if not donut else r * 0.75
        lx    = cx + lr * math.cos(mid_a)
        ly    = cy + lr * math.sin(mid_a)
        pct   = f"{value/total*100:.0f}%"
        parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" font-size="11" fill="{TEXT}" font-weight="600">{pct}</text>')
        angle += sweep

    if donut:
        dr = r * 0.52
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{dr}" fill="{BG}"/>')

    # legend
    ly = h - 20 - len(data) * 16
    if ly < cy + r + 10:
        ly = cy + r + 12
    for i, (label, value) in enumerate(zip(labels, values)):
        col = PALETTE[i % len(PALETTE)]
        lx  = cx - 60
        y   = ly + i * 16
        parts.append(f'<rect x="{lx}" y="{y-8}" width="10" height="10" rx="2" fill="{col}"/>')
        parts.append(f'<text x="{lx+14}" y="{y}" font-size="11" fill="{MUTED}">{_esc(label[:20])} ({_fmt_val(value)})</text>')

    parts.append(_svg_close())
    return "\n".join(parts)


def _sparkline(data, w, h, accent):
    values = [d.get("value", 0) for d in data]
    n      = len(values)
    if n < 2:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"><rect width="{w}" height="{h}" fill="{BG}"/></svg>'
    max_v  = max(values) or 1
    min_v  = min(values)
    rng    = max_v - min_v or 1
    pad    = 4

    def py(v):
        return pad + int((1 - (v - min_v) / rng) * (h - 2 * pad))

    pts = [(int(i * (w - 1) / (n - 1)), py(v)) for i, v in enumerate(values)]
    path = "M" + " L".join(f"{x},{y}" for x, y in pts)
    area = path + f" L{pts[-1][0]},{h} L{pts[0][0]},{h} Z"

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">',
             f'<rect width="{w}" height="{h}" fill="{BG}" rx="4"/>',
             f'<path d="{area}" fill="{accent}" opacity="0.15"/>',
             f'<path d="{path}" fill="none" stroke="{accent}" stroke-width="1.5"/>',
             f'<circle cx="{pts[-1][0]}" cy="{pts[-1][1]}" r="3" fill="{accent}"/>',
             '</svg>']
    return "\n".join(parts)
