"""
Métricas persistentes y reporte de jornada.

`router_status` mostraba solo la sesión del proceso vivo: al reiniciar se
perdía, y cada cliente (Code, Desktop, Cowork) veía únicamente lo suyo. Acá
cada evento queda en una SQLite local compartida, así el reporte suma todos
los clientes y sobrevive a reinicios.

Jornada = eventos contiguos sin una pausa de `gap_s` o más. Al volver de una
pausa larga, take_pending_report() entrega la jornada anterior UNA sola vez
(la marca queda en la DB, así no se repite en otro cliente).

Números honestos: el ahorro es neto del costo fijo (definiciones de tools que
paga cada proceso que arranca). Si un período costó más de lo que ahorró, el
reporte lo dice. No se estima tiempo ni plata: no hay forma de medirlos.
"""

import sqlite3
import time
from collections import Counter
from pathlib import Path

from .clients import friendly

CONTEXT_WINDOW = 200_000
RETENTION_DAYS = 120
MAX_SESSION_LOOKBACK_S = 3 * 24 * 3600

# Eventos de lectura: el ahorro es tokens_source - tokens_delivered.
READ_KINDS = {
    "unchanged": "re-lecturas evitadas",
    "diff": "lecturas por diff",
    "chunks": "lecturas por fragmentos",
    "map": "mapas de archivos grandes",
    "full": "archivos chicos enteros",
}
COUNT_KINDS = {
    "resume": "resumes",
    "stale_memory": "stale_memory_caught",
    "search": "searches",
    "checkpoint_saved": "checkpoints_saved",
    "order_sent": "orders_sent",
    "order_done": "orders_done",
    "compaction_shielded": "compactions_shielded",
}


class Metrics:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, timeout=5)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                tokens_source INTEGER NOT NULL DEFAULT 0,
                tokens_delivered INTEGER NOT NULL DEFAULT 0,
                ref TEXT,
                client TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        self._db.execute("DELETE FROM events WHERE ts < ?", (time.time() - RETENTION_DAYS * 86400,))
        self._db.commit()

    def record(self, kind: str, tokens_source: int = 0, tokens_delivered: int = 0,
               ref: str | None = None, client: str | None = None, ts: float | None = None) -> None:
        self._db.execute(
            "INSERT INTO events (ts, kind, tokens_source, tokens_delivered, ref, client) VALUES (?,?,?,?,?,?)",
            (ts if ts is not None else time.time(), kind, int(tokens_source), int(tokens_delivered), ref, client),
        )
        self._db.commit()

    def events_between(self, start: float, end: float) -> list[dict]:
        rows = self._db.execute(
            "SELECT ts, kind, tokens_source, tokens_delivered, ref, client FROM events "
            "WHERE ts >= ? AND ts <= ? ORDER BY ts", (start, end),
        ).fetchall()
        return [{"ts": r[0], "kind": r[1], "src": r[2], "dlv": r[3], "ref": r[4], "client": r[5]} for r in rows]

    def last_session(self, gap_s: float) -> list[dict]:
        """Eventos de la jornada más reciente (hacia atrás hasta la primera pausa >= gap_s)."""
        row = self._db.execute("SELECT MAX(ts) FROM events").fetchone()
        if not row or row[0] is None:
            return []
        events = self.events_between(row[0] - MAX_SESSION_LOOKBACK_S, row[0])
        start = len(events) - 1
        while start > 0 and events[start]["ts"] - events[start - 1]["ts"] < gap_s:
            start -= 1
        return events[start:]

    def take_pending_report(self, now: float, gap_s: float) -> list[dict] | None:
        """
        Si la última actividad registrada fue hace >= gap_s y esa jornada todavía
        no se reportó, devuelve sus eventos y la marca como reportada.
        """
        row = self._db.execute("SELECT MAX(ts) FROM events").fetchone()
        last = row[0] if row else None
        if last is None or now - last < gap_s:
            return None
        reported = self._db.execute("SELECT value FROM meta WHERE key='reported_until'").fetchone()
        if reported and float(reported[0]) >= last:
            return None
        self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('reported_until', ?)", (str(last),))
        self._db.commit()
        return self.last_session(gap_s)

    def close(self) -> None:
        self._db.close()


def _fmt(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def build_report(events: list[dict]) -> dict | None:
    if not events:
        return None
    saved_by_kind: Counter = Counter()
    count_by_kind: Counter = Counter()
    saved_by_ref: Counter = Counter()
    overhead = 0
    clients: set[str] = set()

    for e in events:
        count_by_kind[e["kind"]] += 1
        if e["client"]:
            clients.add(e["client"])
        if e["kind"] in READ_KINDS:
            saved = max(0, e["src"] - e["dlv"])
            saved_by_kind[e["kind"]] += saved
            if e["ref"]:
                saved_by_ref[e["ref"]] += saved
        elif e["kind"] == "boot":
            overhead += e["dlv"]

    gross = sum(saved_by_kind.values())
    net = gross - overhead
    report = {
        "period": {"from": _fmt(events[0]["ts"]), "to": _fmt(events[-1]["ts"])},
        "clients": sorted({friendly(c) for c in clients}),
        "tokens_kept_out_gross": gross,
        "overhead_tokens": overhead,
        "tokens_saved_net": net,
        "context_windows_200k": round(net / CONTEXT_WINDOW, 2) if net > 0 else 0,
        "by_mechanism": [
            {"kind": k, "how": READ_KINDS[k], "count": count_by_kind[k], "tokens": s, "pct": round(100 * s / gross)}
            for k, s in saved_by_kind.most_common() if s > 0
        ],
        "reads": sum(count_by_kind[k] for k in READ_KINDS),
        "rereads_avoided": count_by_kind["unchanged"],
        "top_files": [{"file": Path(ref).name, "tokens": s}
                      for ref, s in saved_by_ref.most_common(3) if s > 0],
        "overhead_note": "costo fijo estimado: definiciones de tools × procesos iniciados "
                         "(si abriste muchas conversaciones sin reiniciar el MCP, es algo mayor)",
    }
    for kind, field in COUNT_KINDS.items():
        if count_by_kind[kind]:
            report[field] = count_by_kind[kind]
    if net <= 0:
        report["note"] = ("en este período el costo fijo del MCP superó lo ahorrado — rinde con archivos "
                          "grandes y re-lecturas, no en chats cortos")
    return report
