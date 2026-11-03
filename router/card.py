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


def _text(x, y, size, content, fill="#e5e7eb", weight="400", extra="") -> str:
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
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#0b1020"/><stop offset="1" stop-color="#1b1446"/></linearGradient>'
        '<linearGradient id="acc" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#8b5cf6"/><stop offset="1" stop-color="#22d3ee"/></linearGradient></defs>',
        '<rect width="1200" height="630" fill="url(#bg)"/>',
        _text(72, 92, 22, header, fill="#a5b4fc", weight="600", extra='letter-spacing="3"'),
    ]

    if net > 0:
        parts += [
            _text(66, 250, 150, human(net), fill="url(#acc)", weight="800"),
            _text(72, 305, 34, t["subtitle"]),
            _text(72, 352, 26, t["windows"].format(w=report.get("context_windows_200k", 0)), fill="#94a3b8"),
        ]
        y = 400
        for m in report.get("by_mechanism", [])[:3]:
            label = t["mechanisms"].get(m.get("kind"), m.get("how", ""))
            width = max(4, round(4.6 * m["pct"]))
            parts += [
                _text(72, y + 20, 22, label, fill="#cbd5e1"),
                f'<rect x="330" y="{y}" width="460" height="26" rx="13" fill="#ffffff" fill-opacity="0.08"/>',
                f'<rect x="330" y="{y}" width="{width}" height="26" rx="13" fill="url(#acc)"/>',
                _text(806, y + 20, 22, f"{m['pct']}%", fill="#e5e7eb", weight="600"),
            ]
            y += 44
    else:
        parts += [
            _text(66, 250, 150, "0", fill="url(#acc)", weight="800"),
            _text(72, 305, 34, t["subtitle"]),
            _text(72, 360, 26, t["honest"], fill="#fbbf24"),
            _text(72, 398, 22, t["honest_2"], fill="#94a3b8"),
        ]

    y = 170
    for key, label in t["stats"].items():
        value = report.get(key)
        if value:
            parts += [_text(900, y, 54, str(value), fill="#ffffff", weight="800"),
                      _text(900, y + 34, 20, label, fill="#94a3b8")]
            y += 105

    parts += [
        _text(72, 560, 18, t["net"].format(o=human(report.get("overhead_tokens", 0))), fill="#64748b"),
        _text(72, 596, 22, REPO_URL, fill="#a5b4fc", weight="600"),
        _text(1128, 596, 20, t["local"], fill="#64748b", extra='text-anchor="end"'),
        "</svg>",
    ]
    return "\n".join(parts)


def build_card_html(svg: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Continuity MCP card</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; gap: 20px;
         background: #05070f; font-family: {FONT}; color: #e5e7eb; }}
  .card svg {{ width: min(1200px, 94vw); height: auto; border-radius: 16px; box-shadow: 0 20px 60px #000a; }}
  button {{ font: 600 16px {FONT}; padding: 12px 22px; border: 0; border-radius: 999px; cursor: pointer;
           background: linear-gradient(90deg, #8b5cf6, #22d3ee); color: #05070f; }}
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
