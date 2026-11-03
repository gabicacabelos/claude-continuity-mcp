"""
"¿Por qué hicimos esto?" — la historia de decisiones detrás de una línea,
un archivo o un símbolo. Como `git blame`, pero de las decisiones tomadas con
el agente.

Usa lo que la memoria ya guarda: las anclas de los checkpoints (archivo:línea
y símbolos, ver verify.py), los archivos que cada checkpoint registró, las
menciones en el texto y las órdenes cerradas del inbox. Para cada checkpoint
encontrado contrasta sus anclas con el código actual: si la línea cambió, la
decisión puede no aplicar más, y se avisa.
"""

import re
from pathlib import Path

from .verify import PATH_SUFFIXES, verify_anchors

LINE_WINDOW = 20      # una ancla a ±20 líneas cuenta como "esa zona del código"
MAX_CHECKPOINTS = 5
MAX_ORDERS = 3


def _pathlike(s: str) -> bool:
    return Path(s).suffix.lower() in PATH_SUFFIXES or "/" in s or "\\" in s


def parse_ref(ref: str) -> dict:
    ref = ref.strip().strip("`").strip()
    m = re.match(r"^(.*\S):(\d{1,6})$", ref)
    if m and _pathlike(m.group(1)):
        return {"path": m.group(1), "line": int(m.group(2)), "symbol": None}
    if _pathlike(ref):
        return {"path": ref, "line": None, "symbol": None}
    return {"path": None, "line": None, "symbol": ref.removesuffix("()").split(".")[-1]}


def _norm(p: str) -> str:
    return p.replace("\\", "/").lower().rstrip("/")


def _same_file(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return na == nb or na.endswith("/" + nb) or nb.endswith("/" + na)


def _mentions(text: str, needle: str) -> bool:
    return bool(text) and re.search(rf"(?<![\w.]){re.escape(needle)}(?!\w)", text, re.IGNORECASE) is not None


def _match_checkpoint(cp: dict, target: dict) -> tuple[int, list[str], list[dict]]:
    score, reasons, matched = 0, [], []
    texts = [cp.get("summary") or "", *(cp.get("decisions") or []), *(cp.get("open_items") or [])]
    anchors = cp.get("anchors") or []

    if target["symbol"]:
        sym = target["symbol"]
        for a in anchors:
            if a.get("symbol") == sym:
                score, reasons, matched = 3, reasons + [f"ancla del símbolo {sym}"], matched + [a]
        if any(_mentions(t, sym) for t in texts):
            score = max(score, 1)
            reasons.append("mencionado en el texto")
        return score, reasons, matched

    path, line = target["path"], target["line"]
    for a in anchors:
        if a.get("path") and _same_file(a["path"], path):
            matched.append(a)
            if line and a.get("line") and abs(a["line"] - line) <= LINE_WINDOW:
                score = 3
                reasons.append(f"ancla en la línea {a['line']}")
            else:
                score = max(score, 2)
                reasons.append("ancla en el archivo")
    if any(_same_file(f["path"], path) for f in cp.get("files") or []):
        score = max(score, 2)
        reasons.append("archivo registrado en el checkpoint")
    if any(_mentions(t, Path(path).name) for t in texts):
        score = max(score, 1)
        reasons.append("mencionado en el texto")
    return score, reasons, matched


def find_history(ref: str, checkpoints: list[dict], orders: list[dict]) -> dict:
    target = parse_ref(ref)
    found = []
    for cp in checkpoints:
        score, reasons, matched = _match_checkpoint(cp, target)
        if not score:
            continue
        entry = {
            "name": cp.get("name"),
            "saved_at": cp.get("saved_at"),
            "matched": list(dict.fromkeys(reasons)),
            "summary": (cp.get("summary") or "")[:300],
            "decisions": (cp.get("decisions") or [])[:5],
        }
        if matched:
            check = verify_anchors(matched)
            entry["still_valid"] = not check["issues"]
            if check["issues"]:
                entry["drift"] = check["issues"]
        found.append((score, cp.get("ts") or 0, entry))
    found.sort(key=lambda x: (x[0], x[1]), reverse=True)

    needle = target["symbol"] or Path(target["path"]).name
    hits = [{"id": o["id"], "done": o.get("done"), "message": (o.get("message") or "")[:200],
             "result": (o.get("result") or "")[:200], "completed_by": o.get("completed_by")}
            for o in orders if _mentions(o.get("message") or "", needle) or _mentions(o.get("result") or "", needle)]
    return {"target": target, "checkpoints": [e for _, _, e in found[:MAX_CHECKPOINTS]], "orders": hits[:MAX_ORDERS]}
