#!/usr/bin/env python3
"""Giro del motore su GitHub Actions. GitHub avvia i giri programmati in modo irregolare (anche 1 volta l'ora),
quindi ogni giro resta acceso ~50 minuti e lavora minuto per minuto: legge i comandi di Claude, rigioca le candele,
manda subito gli avvisi su Telegram, il report una volta all'ora, e ripubblica lo stato. Nessun dato resta nel repository."""
import datetime as dt, os, time
open("server.flag", "w").write("1")
import sfida_engine as e, sync

DURATA_MIN = int(os.environ.get("SFIDA_LOOP_MIN", "50"))
sync.pull_state(any_origin=True)
t_end = time.time() + DURATA_MIN * 60
last_push = 0
while True:
    try:
        S = e.load()
        n = sync.apply_cmds(S)
        ev0 = os.path.getsize(e.EVENTS) if os.path.exists(e.EVENTS) else 0
        e.cmd_tick(None)
        ev1 = os.path.getsize(e.EVENTS) if os.path.exists(e.EVENTS) else 0
        S = e.load()
        h = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H")
        if S.get("_last_report_h") != h:
            e.cmd_report(None)
            S = e.load(); S["_last_report_h"] = h; e.save(S)
        if n or ev1 != ev0 or time.time() - last_push > 600:
            sync.push_state(); last_push = time.time()
    except Exception as ex:
        print("errore nel giro:", ex)
    if time.time() > t_end:
        break
    time.sleep(60 - time.time() % 60 + 2)
sync.push_state()
