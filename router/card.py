"""
Tarjeta compartible del ahorro (1200×630, tamaño de preview de redes).

Se genera 100% local a partir de un reporte de metrics.build_report(): un SVG
autocontenido y un HTML con un botón que lo exporta a PNG en el navegador
(X, Reddit y LinkedIn no aceptan SVG). Sin dependencias.

Privacidad: la tarjeta está pensada para publicarse, así que NUNCA incluye
nombres de archivos, rutas ni proyectos — solo números agregados y los
agentes usados. Honestidad: el número es neto del costo fijo del MCP; si el
período costó más de lo que ahorró, la tarjeta lo dice.
"""

from xml.sax.saxutils import escape

REPO_URL = "github.com/gabicacabelos/claude-continuity-mcp"
FONT = "Inter, 'Segoe UI', Helvetica, Arial, sans-serif"

# Paleta de INDIVIDRA (individratec.com): plana, sin degradés.
OBSIDIAN = "#0C0E12"
OBSIDIAN_DEEP = "#070809"
EMBER = "#FF5520"
EMBER_LIGHT = "#FFA07A"
TEXT = "#ECEDEE"
TEXT_MUTED = "#9A9EA5"
TEXT_FAINT = "#6B7078"

TEXTS = {
    "en": {
        "period": {"today": "TODAY", "week": "THIS WEEK", "last_session": "LAST SESSION"},
        "subtitle": "tokens your AI agent didn't have to read",
        "windows": "≈ {w} context windows of 200k",
        "net": "net of the MCP's fixed cost ({o} tokens) · measured on your machine",
        "honest": "the MCP's fixed cost exceeded its savings this period",
        "honest_2": "it pays off with big files and re-reads, not short chats",
        "local": "100% local · no API keys",
        "mechanisms": {"unchanged": "re-reads avoided", "diff": "diff-only reads", "chunks": "fragment reads",
                       "map": "file maps", "full": "small files"},
        "stats": {"rereads_avoided": "re-reads avoided", "compactions_shielded": "compactions shielded",
                  "stale_memory_caught": "stale memories caught", "resumes": "tasks resumed"},
    },
    "es": {
        "period": {"today": "HOY", "week": "ESTA SEMANA", "last_session": "ÚLTIMA JORNADA"},
        "subtitle": "tokens que tu agente de IA no tuvo que leer",
        "windows": "≈ {w} ventanas de contexto de 200k",
        "net": "neto del costo fijo del MCP ({o} tokens) · medido en tu máquina",
        "honest": "en este período el costo fijo del MCP superó lo ahorrado",
        "honest_2": "rinde con archivos grandes y re-lecturas, no en chats cortos",
        "local": "100% local · sin API keys",
        "mechanisms": {"unchanged": "re-lecturas evitadas", "diff": "lecturas por diff",
                       "chunks": "lecturas por fragmentos", "map": "mapas de archivos", "full": "archivos chicos"},
        "stats": {"rereads_avoided": "re-lecturas evitadas", "compactions_shielded": "compactaciones protegidas",
                  "stale_memory_caught": "memorias viejas detectadas", "resumes": "tareas retomadas"},
    },
}


def human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(max(0, n))


def _text(x, y, size, content, fill=TEXT, weight="400", extra="") -> str:
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" {extra}>{escape(content)}</text>')


def build_card_svg(report: dict, period: str = "week", lang: str = "en") -> str:
    t = TEXTS.get(lang, TEXTS["en"])
    net = report.get("tokens_saved_net", 0)
    header = f"CONTINUITY MCP · {t['period'].get(period, period.upper())}"
    if report.get("clients"):
        header += " · " + " + ".join(report["clients"])

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">',
        f'<rect width="1200" height="630" fill="{OBSIDIAN}"/>',
        f'<rect x="0" y="0" width="12" height="630" fill="{EMBER}"/>',
        _text(72, 92, 22, header, fill=EMBER, weight="600", extra='letter-spacing="3"'),
    ]

    if net > 0:
        parts += [
            _text(66, 250, 150, human(net), fill=EMBER, weight="800"),
            _text(72, 305, 34, t["subtitle"]),
            _text(72, 352, 26, t["windows"].format(w=report.get("context_windows_200k", 0)), fill=TEXT_MUTED),
        ]
        y = 400
        for m in report.get("by_mechanism", [])[:3]:
            label = t["mechanisms"].get(m.get("kind"), m.get("how", ""))
            width = max(4, round(4.6 * m["pct"]))
            parts += [
                _text(72, y + 20, 22, label, fill=TEXT),
                f'<rect x="330" y="{y}" width="460" height="26" rx="2" fill="#ffffff" fill-opacity="0.08"/>',
                f'<rect x="330" y="{y}" width="{width}" height="26" rx="2" fill="{EMBER}"/>',
                _text(806, y + 20, 22, f"{m['pct']}%", fill=TEXT, weight="600"),
            ]
            y += 44
    else:
        parts += [
            _text(66, 250, 150, "0", fill=EMBER, weight="800"),
            _text(72, 305, 34, t["subtitle"]),
            _text(72, 360, 26, t["honest"], fill=EMBER_LIGHT),
            _text(72, 398, 22, t["honest_2"], fill=TEXT_MUTED),
        ]

    y = 170
    for key, label in t["stats"].items():
        value = report.get(key)
        if value:
            parts += [_text(900, y, 54, str(value), fill=TEXT, weight="800"),
                      _text(900, y + 34, 20, label, fill=TEXT_MUTED)]
            y += 105

    parts += [
        _text(72, 560, 18, t["net"].format(o=human(report.get("overhead_tokens", 0))), fill=TEXT_FAINT),
        _text(72, 596, 22, REPO_URL, fill=EMBER, weight="600"),
        _text(1128, 596, 20, t["local"], fill=TEXT_FAINT, extra='text-anchor="end"'),
        "</svg>",
    ]
    return "\n".join(parts)


def build_card_html(svg: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Continuity MCP card</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; gap: 20px;
         background: {OBSIDIAN_DEEP}; font-family: {FONT}; color: {TEXT}; }}
  .card svg {{ width: min(1200px, 94vw); height: auto; border-radius: 4px; box-shadow: 0 20px 60px #000a; }}
  button {{ font: 600 16px {FONT}; padding: 12px 22px; border: 0; border-radius: 4px; cursor: pointer;
           background: {EMBER}; color: #fff; }}
</style></head>
<body>
<div class="card">{svg}</div>
<button id="dl">Download PNG</button>
<script>
document.getElementById('dl').onclick = () => {{
  const svg = document.querySelector('.card svg');
  const data = new XMLSerializer().serializeToString(svg);
  const img = new Image();
  img.onload = () => {{
    const c = document.createElement('canvas'); c.width = 1200; c.height = 630;
    c.getContext('2d').drawImage(img, 0, 0, 1200, 630);
    c.toBlob(b => {{ const a = document.createElement('a'); a.href = URL.createObjectURL(b);
                     a.download = 'continuity-card.png'; a.click(); }}, 'image/png');
  }};
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(data);
}};
</script>
</body></html>
"""
