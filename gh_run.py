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
        now = dt.datetime.now(dt.timezone.utc)
        h = now.strftime("%Y-%m-%d %H")
        n_ev = sum(1 for _ in open(e.EVENTS, encoding="utf-8")) if os.path.exists(e.EVENTS) else 0
        if now.hour == 20 and S.get("_last_daily") != now.strftime("%Y-%m-%d"):
            e.cmd_daily(None)                                   # riepilogo del giorno alle 21 italiane
            S = e.load(); S["_last_daily"] = now.strftime("%Y-%m-%d"); S["_last_report_h"] = h; S["_ev_at_report"] = n_ev; e.save(S)
        elif S.get("_last_report_h") != h and (n_ev != S.get("_ev_at_report") or now.hour % 4 == 0):
            e.cmd_report(None)                                  # aggiornamento solo se è successo qualcosa (o ogni 4 ore)
            S = e.load(); S["_last_report_h"] = h; S["_ev_at_report"] = n_ev; e.save(S)
        if n or ev1 != ev0 or time.time() - last_push > 600:
            sync.push_state(); last_push = time.time()
    except Exception as ex:
        print("errore nel giro:", ex)
    if time.time() > t_end:
        break
    time.sleep(60 - time.time() % 60 + 2)
sync.push_state()
