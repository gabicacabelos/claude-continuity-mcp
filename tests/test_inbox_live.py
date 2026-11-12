"""
v3.9 — buzón "en vivo": identidad de Antigravity, claim exclusivo, check propio por
defecto, aviso automático (inbox_alert), claude-continuity-watch y hook de SessionStart.
"""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from router import clients
from router.inbox import CLAIM_TTL_SECONDS, Inbox
from router.watch import main as watch_main
from router.watch import watch

REPO = Path(__file__).parent.parent


@pytest.fixture
def ib(tmp_path):
    box = Inbox(str(tmp_path / "inbox.db"))
    yield box
    box.close()


# ─── identidad ────────────────────────────────────────────────────────────────

def test_antigravity_client_name_maps_to_antigravity():
    # Antigravity se presenta como "antigravity-client"; antes quedaba con ese id y
    # no veía las órdenes dirigidas a "antigravity".
    assert clients.client_id("antigravity-client") == "antigravity"
    assert clients.client_id("Antigravity") == "antigravity"


# ─── claim ────────────────────────────────────────────────────────────────────

def test_claim_is_exclusive_and_hides_order_from_others(ib):
    oid = ib.send("revisar PR", to_client="any", from_client="cowork")
    assert ib.claim(oid, "code")["claimed"] is True
    second = ib.claim(oid, "antigravity")
    assert second["claimed"] is False and "code" in second["reason"]
    assert [o["id"] for o in ib.check("antigravity")] == []          # otro agente ya no la ve
    mine = ib.check("code")
    assert mine[0]["id"] == oid and mine[0]["claimed_by"] == "code"  # el que la tomó sí
    assert ib.claim(oid, "code")["claimed"] is True                  # re-claim propio es idempotente


def test_expired_claim_can_be_taken_again(ib):
    oid = ib.send("tarea", to_client="any")
    ib.claim(oid, "code")
    ib._db.execute("UPDATE inbox SET claimed_at=? WHERE id=?", (time.time() - CLAIM_TTL_SECONDS - 5, oid))
    ib._db.commit()
    assert ib.claim(oid, "antigravity")["claimed"] is True


def test_claim_race_between_two_processes_has_one_winner(tmp_path):
    db = str(tmp_path / "inbox.db")
    first = Inbox(db)
    oid = first.send("una sola vez", to_client="any")
    first.close()
    results = []
    barrier = threading.Barrier(6)

    def grab(name):
        box = Inbox(db)              # una conexión por "agente", como procesos separados
        barrier.wait()
        results.append(box.claim(oid, name)["claimed"])
        box.close()

    threads = [threading.Thread(target=grab, args=(f"agent{i}",)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1


def test_claim_unknown_or_done_order(ib):
    assert ib.claim(999, "code")["reason"] == "no existe"
    oid = ib.send("x", to_client="code")
    ib.complete(oid)
    assert ib.claim(oid, "code")["reason"] == "ya está completada"


def test_history_ties_are_ordered_by_id(ib, monkeypatch):
    # En Windows dos órdenes completadas en el mismo tick tenían el mismo done_at.
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    ids = []
    for i in range(4):
        oid = ib.send(f"o{i}", to_client="code")
        ib.complete(oid)
        ids.append(oid)
    assert [h["id"] for h in ib.history(limit=4)] == list(reversed(ids))


# ─── watch ────────────────────────────────────────────────────────────────────

def test_watch_wakes_up_on_new_order(ib):
    ib.send("ya estaba", to_client="code")                      # pendiente previa: no despierta

    def later():
        time.sleep(0.4)
        ib2 = Inbox(ib.db_path)
        ib2.send("orden nueva para vos", to_client="code", from_client="antigravity")
        ib2.close()

    threading.Thread(target=later).start()
    code, payload = watch(ib, "code", None, timeout=10, interval=0.1, include_existing=False)
    assert code == 0 and payload["event"] == "new_orders"
    assert [o["message"] for o in payload["orders"]] == ["orden nueva para vos"]
    assert "NO CONFIABLE" in payload["trust"]


def test_watch_ignores_own_orders_and_times_out(ib):
    ib.send("la mandé yo", to_client="any", from_client="code")
    fake = {"t": 0.0}
    code, payload = watch(ib, "code", None, timeout=5, interval=1, include_existing=True,
                          clock=lambda: fake["t"], sleep=lambda s: fake.update(t=fake["t"] + s))
    assert code == 3 and payload["event"] == "timeout"


def test_watch_returns_reply_to_a_specific_order(ib):
    oid = ib.send("hacé el mockup", to_client="antigravity", from_client="code")

    def later():
        time.sleep(0.3)
        ib2 = Inbox(ib.db_path)
        ib2.complete(oid, result="mockup listo", completed_by="antigravity")
        ib2.close()

    threading.Thread(target=later).start()
    code, payload = watch(ib, "code", oid, timeout=10, interval=0.1, include_existing=False)
    assert code == 0 and payload["order"]["result"] == "mockup listo"


def test_watch_reply_in_same_clock_tick_is_not_lost(ib, monkeypatch):
    # Reloj congelado: la respuesta tiene el MISMO timestamp que el arranque del watch.
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    oid = ib.send("pregunta", to_client="antigravity", from_client="code")
    fake = {"n": 0}

    def sleep(_):
        fake["n"] += 1
        if fake["n"] == 1:
            ib.complete(oid, result="respuesta", completed_by="antigravity")

    code, payload = watch(ib, "code", None, timeout=60, interval=1, include_existing=False,
                          clock=lambda: 1_800_000_000.0 + fake["n"], sleep=sleep)
    assert code == 0 and payload["event"] == "reply" and payload["orders"][0]["id"] == oid


def test_watch_cli_without_inbox_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONTINUITY_HOME", str(tmp_path / "nada"))
    assert watch_main(["--to", "code", "--timeout", "0"]) == 2
    assert "no hay buzón" in capsys.readouterr().out


# ─── hook SessionStart ───────────────────────────────────────────────────────

def _run_hook(home: Path, source: str) -> str:
    env = {**os.environ, "CLAUDE_CONTINUITY_HOME": str(home)}
    p = subprocess.run([sys.executable, str(REPO / "hooks" / "inbox_peek.py")],
                       input=json.dumps({"source": source, "session_id": "s1"}),
                       capture_output=True, text=True, env=env, timeout=30)
    return p.stdout


def test_hook_injects_pending_orders_for_code(tmp_path):
    box = Inbox(str(tmp_path / "cache" / "inbox.db"))
    box.send("revisá el buzón: plan del video", to_client="code", from_client="antigravity")
    box.send("esto lo mandé yo", to_client="any", from_client="code")
    box.close()
    out = _run_hook(tmp_path, "startup")
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "1 pending inbox order" in ctx and "plan del video" in ctx
    assert "esto lo mandé yo" not in ctx and "UNTRUSTED" in ctx


def test_hook_is_silent_without_orders_or_on_compact(tmp_path):
    Inbox(str(tmp_path / "cache" / "inbox.db")).close()
    assert _run_hook(tmp_path, "startup") == ""
    box = Inbox(str(tmp_path / "cache" / "inbox.db"))
    box.send("algo", to_client="code", from_client="antigravity")
    box.close()
    assert _run_hook(tmp_path, "compact") == ""


# ─── server: check propio por defecto + inbox_alert ──────────────────────────

@pytest.fixture(scope="module")
def srv():
    spec = importlib.util.spec_from_file_location("continuity_server_live", REPO / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_check_without_to_shows_only_my_orders(srv, monkeypatch):
    monkeypatch.setattr(srv, "_me", lambda: "code")
    srv.inbox.send("para antigravity", to_client="antigravity", from_client="cowork")
    oid = srv.inbox.send("para code", to_client="code", from_client="cowork")
    out = json.loads(asyncio.run(srv.router_inbox(srv.InboxInput(action="check"))))
    ids = [o["id"] for o in out["orders"]]
    assert oid in ids and all(o["to"] in ("code", "any") for o in out["orders"])


def test_claim_action_via_tool(srv, monkeypatch):
    monkeypatch.setattr(srv, "_me", lambda: "code")
    oid = srv.inbox.send("tomame", to_client="any", from_client="cowork")
    out = json.loads(asyncio.run(srv.router_inbox(srv.InboxInput(action="claim", order_id=oid))))
    assert out["status"] == "claimed" and out["by"] == "code"


def test_inbox_alert_reports_only_new_orders_and_replies(srv, monkeypatch):
    monkeypatch.setattr(srv, "_me", lambda: "antigravity")
    srv._alert_state.update(seen=set(srv.inbox.pending_ids("antigravity", exclude_from="antigravity")),
                            seen_replies=set(), since=time.time(), last_query=0.0)
    new_id = srv.inbox.send("necesito el plan técnico", to_client="antigravity", from_client="code")
    alert = srv._inbox_alert()
    assert alert and alert["new_orders"] == [new_id]
    assert srv._inbox_alert() is None                    # throttle de 20 s
    srv._alert_state["last_query"] = 0.0
    assert srv._inbox_alert() is None                    # nada NUEVO: no repite el aviso
    mine = srv.inbox.send("pregunta de antigravity", to_client="code", from_client="antigravity")
    srv.inbox.complete(mine, result="respondido", completed_by="code")
    srv._alert_state["last_query"] = 0.0
    alert = srv._inbox_alert()
    assert alert["replies"] == [{"id": mine, "by": "code"}]
    srv._alert_state["last_query"] = 0.0
    assert srv._inbox_alert() is None                    # la misma respuesta no se avisa dos veces


def test_inbox_alert_rides_on_any_tool_response(srv, monkeypatch):
    monkeypatch.setattr(srv, "_me", lambda: "cursor")
    srv._alert_state.update(seen=set(), since=time.time(), last_query=0.0)
    srv.inbox.send("orden para cursor", to_client="cursor", from_client="code")
    payload = json.loads(srv._j({"status": "ok"}))
    assert payload["inbox_alert"]["new_orders"]
