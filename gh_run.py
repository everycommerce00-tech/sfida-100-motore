#!/usr/bin/env python3
"""Un giro del motore su GitHub Actions: prende lo stato da Telegram, applica i comandi di Claude,
rigioca i minuti, manda il report una volta all'ora e ripubblica lo stato. Nessun dato resta nel repository."""
import datetime as dt, os
open("server.flag", "w").write("1")
import sfida_engine as e, sync
sync.pull_state(any_origin=True)
S = e.load()
sync.apply_cmds(S)
e.cmd_tick(None)
S = e.load()
h = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H")
if S.get("_last_report_h") != h:
    e.cmd_report(None)
    S = e.load(); S["_last_report_h"] = h; e.save(S)
sync.push_state()
