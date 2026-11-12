"""
Inbox: cola de órdenes cruzadas entre clientes de Claude.

Cowork, Claude Code, Desktop y Claude Design no pueden comandarse entre sí en
tiempo real — pero comparten este disco. El inbox es el buzón asíncrono: un
cliente deja una orden (opcionalmente vinculada a un checkpoint con todo el
contexto de la tarea y a assets de handoff), otro cliente la consume, la
ejecuta y reporta el resultado.

Flujo típico (código ↔ diseño):
  [Code]    inbox send to=design message="hacé el hero de la landing"
                       assets=["/proj/brief.md","https://.../wireframe.png"]
  [Design]  inbox check to=design  → ve la orden, el checkpoint y los assets
  [Design]  inbox complete id=1 result="mockup listo"
                       assets=["https://.../hero-v1.fig","/exports/hero.png"]
  [Code]    inbox history          → ve el resultado + los assets devueltos
"""

import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DONE_RETENTION_DAYS = 30

# Versión del schema (PRAGMA user_version) — misma estrategia que el ledger.
INBOX_SCHEMA_VERSION = 2

# Clientes reconocidos del "pack". El inbox acepta cualquier string como
# destino (texto libre), pero estos son los roles de primera clase que
# los clientes chequean por convención al arrancar una sesión.
KNOWN_CLIENTS = ("cowork", "code", "desktop", "design", "antigravity", "any")

# Una orden tomada (claim) por un agente que después se cayó no puede quedar
# bloqueada para siempre: pasado este plazo vuelve a estar libre.
CLAIM_TTL_SECONDS = 4 * 3600

_COLS = "id, to_client, from_client, message, checkpoint, assets, completed_by, created_at, claimed_by, claimed_at"


class Inbox:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path)
        # El inbox es la DB genuinamente multi-proceso: Code, Desktop y Cowork
        # tienen cada uno su server.py contra este mismo archivo. En modo
        # rollback los lectores bloquean escritores; WAL los deja convivir.
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=3000")
        except Exception as e:
            logger.warning(f"no se pudo activar WAL en el inbox: {e}")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                to_client TEXT NOT NULL DEFAULT 'any',
                from_client TEXT NOT NULL DEFAULT 'unknown',
                message TEXT NOT NULL,
                checkpoint TEXT,
                assets TEXT,
                result_assets TEXT,
                completed_by TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                result TEXT,
                created_at REAL NOT NULL,
                done_at REAL
            )
        """)
        self._migrate()
        self._db.commit()
        try:
            cutoff = time.time() - DONE_RETENTION_DAYS * 86400
            self._db.execute("DELETE FROM inbox WHERE status='done' AND done_at < ?", (cutoff,))
            self._db.commit()
        except Exception as e:
            logger.warning(f"purga de inbox falló: {e}")

    def _migrate(self) -> None:
        """Agrega columnas nuevas a DBs viejas sin romperlas (ALTER idempotente)."""
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(inbox)").fetchall()}
        for col, kind in (("assets", "TEXT"), ("result_assets", "TEXT"), ("completed_by", "TEXT"),
                          ("claimed_by", "TEXT"), ("claimed_at", "REAL")):
            if col not in cols:
                try:
                    self._db.execute(f"ALTER TABLE inbox ADD COLUMN {col} {kind}")
                except sqlite3.OperationalError as e:
                    logger.warning(f"migración inbox ({col}) falló: {e}")
        # Versionado con PRAGMA user_version (misma estrategia que el ledger):
        # migraciones futuras se encadenan por número de versión.
        try:
            v = self._db.execute("PRAGMA user_version").fetchone()[0]
            if v < INBOX_SCHEMA_VERSION:
                # v2 (claim): columnas claimed_by/claimed_at, agregadas arriba de forma idempotente.
                self._db.execute(f"PRAGMA user_version = {INBOX_SCHEMA_VERSION}")
                if v > 0:
                    logger.info(f"Inbox migrado de schema v{v} a v{INBOX_SCHEMA_VERSION}")
        except Exception as e:
            logger.warning(f"migración de schema del inbox falló: {e}")

    @staticmethod
    def _dump_assets(assets) -> str | None:
        """Normaliza una lista de assets (rutas/URLs) a JSON, o None."""
        if not assets:
            return None
        if isinstance(assets, str):
            assets = [assets]
        clean = [str(a).strip() for a in assets if str(a).strip()]
        return json.dumps(clean, ensure_ascii=False) if clean else None

    @staticmethod
    def _load_assets(raw) -> list[str]:
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []

    def find_pending_duplicate(self, to_client: str, message: str) -> dict | None:
        """
        Busca una orden PENDIENTE con el mismo destino exacto y el mismo mensaje
        (normalizado: espacios colapsados, sin distinguir mayúsculas). Evita que
        una orden se acumule dos veces en el buzón mientras la anterior sigue sin
        resolver. No hace match contra 'any' cruzado — el destino debe ser idéntico.
        """
        to_client = (to_client or "any").lower().strip()
        norm = " ".join(message.split()).casefold()
        rows = self._db.execute(
            "SELECT id, to_client, from_client, message, checkpoint, assets, created_at FROM inbox "
            "WHERE status='pending' AND to_client=?",
            (to_client,),
        ).fetchall()
        for r in rows:
            if " ".join(r[3].split()).casefold() == norm:
                return {
                    "id": r[0], "to": r[1], "from": r[2], "message": r[3], "checkpoint": r[4],
                    "assets": self._load_assets(r[5]),
                    "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[6])),
                }
        return None

    def send(self, message: str, to_client: str = "any", from_client: str = "unknown",
             checkpoint: str | None = None, assets=None, allow_duplicate: bool = False) -> int:
        to_client = to_client.lower().strip() or "any"
        if not allow_duplicate:
            dup = self.find_pending_duplicate(to_client, message)
            if dup:
                logger.info(f"inbox.send: orden duplicada evitada — reusando id {dup['id']} (to='{to_client}')")
                return dup["id"]
        cur = self._db.execute(
            "INSERT INTO inbox (to_client, from_client, message, checkpoint, assets, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (to_client, from_client.lower().strip() or "unknown",
             message, checkpoint, self._dump_assets(assets), time.time()),
        )
        self._db.commit()
        return cur.lastrowid

    def _row(self, r) -> dict:
        claimed = r[8] if (r[8] and r[9] and r[9] > time.time() - CLAIM_TTL_SECONDS) else None
        out = {"id": r[0], "to": r[1], "from": r[2], "message": r[3], "checkpoint": r[4],
               "assets": self._load_assets(r[5]), "completed_by": r[6],
               "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[7]))}
        if claimed:
            out["claimed_by"] = claimed
        return out

    def check(self, to_client: str | None = None, include_claimed_by_others: bool = False) -> list[dict]:
        """
        Órdenes pendientes para un cliente (incluye las dirigidas a 'any').
        Por defecto oculta las que ya tomó OTRO agente (claim vigente): así dos
        agentes no ejecutan la misma orden 'any'.
        """
        fresh = time.time() - CLAIM_TTL_SECONDS
        if to_client:
            me = to_client.lower().strip()
            sql = (f"SELECT {_COLS} FROM inbox WHERE status='pending' AND (to_client=? OR to_client='any')")
            args: list = [me]
            if not include_claimed_by_others:
                sql += " AND (claimed_by IS NULL OR claimed_by=? OR claimed_at < ?)"
                args += [me, fresh]
            rows = self._db.execute(sql + " ORDER BY created_at, id", args).fetchall()
        else:
            rows = self._db.execute(
                f"SELECT {_COLS} FROM inbox WHERE status='pending' ORDER BY created_at, id").fetchall()
        return [self._row(r) for r in rows]

    def claim(self, order_id: int, by: str) -> dict:
        """
        Toma una orden de forma atómica (un solo UPDATE condicional): si dos agentes
        la reclaman a la vez, gana uno solo. Un claim vencido (CLAIM_TTL) se puede retomar.
        """
        by = (by or "unknown").lower().strip()
        now = time.time()
        cur = self._db.execute(
            "UPDATE inbox SET claimed_by=?, claimed_at=? WHERE id=? AND status='pending' "
            "AND (claimed_by IS NULL OR claimed_by=? OR claimed_at < ?)",
            (by, now, order_id, by, now - CLAIM_TTL_SECONDS),
        )
        self._db.commit()
        if cur.rowcount:
            return {"claimed": True, "id": order_id, "by": by}
        row = self._db.execute("SELECT status, claimed_by FROM inbox WHERE id=?", (order_id,)).fetchone()
        if not row:
            return {"claimed": False, "id": order_id, "reason": "no existe"}
        if row[0] != "pending":
            return {"claimed": False, "id": order_id, "reason": "ya está completada"}
        return {"claimed": False, "id": order_id, "reason": f"ya la tomó '{row[1]}'"}

    def pending_ids(self, to_client: str, exclude_from: str | None = None) -> list[int]:
        """Ids pendientes para un cliente (sin las tomadas por otros), barato: para avisos automáticos."""
        me = to_client.lower().strip()
        rows = self._db.execute(
            "SELECT id FROM inbox WHERE status='pending' AND (to_client=? OR to_client='any') "
            "AND (claimed_by IS NULL OR claimed_by=? OR claimed_at < ?) AND from_client != ? ORDER BY id",
            (me, me, time.time() - CLAIM_TTL_SECONDS, (exclude_from or "").lower()),
        ).fetchall()
        return [r[0] for r in rows]

    def replies_since(self, from_client: str, since: float) -> list[dict]:
        """Órdenes que ESTE cliente mandó y que alguien completó después de `since` (respuestas)."""
        rows = self._db.execute(
            # >= (no >): con ~15 ms de resolución del reloj en Windows, una respuesta del mismo
            # tick se perdía. Los que llaman deduplican por id.
            "SELECT id, completed_by, done_at FROM inbox WHERE status='done' AND from_client=? AND done_at >= ? "
            "ORDER BY done_at, id",
            (from_client.lower().strip(), since),
        ).fetchall()
        return [{"id": r[0], "completed_by": r[1], "done_at": r[2]} for r in rows]

    def get(self, order_id: int) -> dict | None:
        r = self._db.execute(
            "SELECT id, to_client, from_client, message, status, result, result_assets, completed_by, done_at "
            "FROM inbox WHERE id=?", (order_id,)).fetchone()
        if not r:
            return None
        return {"id": r[0], "to": r[1], "from": r[2], "message": r[3], "status": r[4], "result": r[5],
                "result_assets": self._load_assets(r[6]), "completed_by": r[7], "done_at": r[8]}

    def complete(self, order_id: int, result: str | None = None, assets=None,
                 completed_by: str | None = None) -> bool:
        """
        completed_by: quién EJECUTÓ la orden — independiente de to_client (para
        quién era). No son lo mismo: una orden con to_client='design' puede
        terminar completada por un cliente distinto de Claude Design (ej. Code
        haciendo de puente). Sin este campo ese desvío queda invisible. Si no
        se pasa, queda NULL (backward-compat — no rompe llamadas viejas).
        """
        by = completed_by.lower().strip() if completed_by else None
        cur = self._db.execute(
            "UPDATE inbox SET status='done', result=?, result_assets=?, completed_by=?, done_at=? "
            "WHERE id=? AND status='pending'",
            (result, self._dump_assets(assets), by, time.time(), order_id),
        )
        self._db.commit()
        return cur.rowcount > 0

    def history(self, limit: int = 10) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, to_client, from_client, message, result, result_assets, completed_by, done_at FROM inbox "
            # id desempata: en Windows dos órdenes completadas en el mismo tick (~15 ms)
            # tienen el mismo done_at y el orden salía al azar (test intermitente).
            "WHERE status='done' ORDER BY done_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            to_client, completed_by = r[1], r[6]
            entry = {
                "id": r[0], "to": to_client, "from": r[2], "message": r[3], "result": r[4],
                "result_assets": self._load_assets(r[5]), "completed_by": completed_by,
                "done": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[7])) if r[7] else None,
            }
            # Visibiliza el caso #17: orden dirigida a X, ejecutada por Y.
            if completed_by and to_client != "any" and completed_by != to_client:
                entry["executed_by_different_client"] = True
            out.append(entry)
        return out

    def close(self) -> None:
        self._db.close()
