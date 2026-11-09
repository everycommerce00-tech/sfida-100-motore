#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ponte tra il "cervello" (controlli orari di Claude) e il server che fa girare il motore ogni minuto.
Tutto passa dal bot Telegram della sfida, quindi non servono porte aperte né chiavi in più:

  canale COMANDI : Claude fissa (pin) un messaggio JSON con i comandi; il server lo legge ogni minuto.
  canale STATO   : il server fissa un archivio con state.json e i CSV; Claude lo scarica a ogni controllo.

Uso (lato Claude):   python3 sync.py pull          scarica lo stato del server
                     python3 sync.py push          invia i comandi in coda (cmds_pending.json)
Uso (lato server):   python3 sync.py minute        legge comandi, tick, report orario, pubblica stato
Impostazioni in .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SFIDA_CMD_CHAT, SFIDA_STATE_CHAT
"""
import io, json, os, sys, tarfile, time, datetime as dt
import requests
import sfida_engine as e

FILES = ["state.json", "trades.csv", "equity.csv", "events.csv", "registro.md"]
PENDING = e.P("cmds_pending.json")

def cfg():
    v = e.env()
    return v["TELEGRAM_BOT_TOKEN"], v.get("SFIDA_CMD_CHAT"), v.get("SFIDA_STATE_CHAT")

def api(method, **kw):
    tok, _, _ = cfg()
    files = kw.pop("files", None)
    r = requests.post(f"https://api.telegram.org/bot{tok}/{method}", data=kw, files=files, timeout=60).json()
    if not r.get("ok"):
        raise RuntimeError(f"{method}: {r}")
    return r["result"]

def pinned(chat):
    return api("getChat", chat_id=chat).get("pinned_message")

# ------------------------------------------------------------------ stato
def push_state():
    _, _, st = cfg()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for f in FILES:
            if os.path.exists(e.P(f)):
                t.add(e.P(f), arcname=f)
    buf.seek(0)
    msg = api("sendDocument", chat_id=st, disable_notification=True,
              caption=f"stato {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z da {'server' if os.path.exists(e.P('server.flag')) else 'claude'}",
              files={"document": ("stato.tar.gz", buf)})
    api("pinChatMessage", chat_id=st, message_id=msg["message_id"], disable_notification=True)
    old = e.load().get("_last_state_msg")
    if old:
        try: api("deleteMessage", chat_id=st, message_id=old)
        except Exception: pass
    S = e.load(); S["_last_state_msg"] = msg["message_id"]; e.save(S)

def server_live():
    _, _, st = cfg()
    pm = pinned(st)
    return bool(pm and "document" in pm and "da server" in (pm.get("caption") or "")), pm

def pull_state(any_origin=False):
    live, pm = server_live()
    if not live and not (any_origin and pm and "document" in pm):
        sys.exit("il server non ha ancora pubblicato uno stato")
    f = api("getFile", file_id=pm["document"]["file_id"])
    tok, _, _ = cfg()
    data = requests.get(f"https://api.telegram.org/file/bot{tok}/{f['file_path']}", timeout=60).content
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        for m in t.getmembers():
            if m.name in FILES:
                with open(e.P(m.name), "wb") as out:
                    out.write(t.extractfile(m).read())
    print("stato scaricato:", pm.get("caption"))

# ------------------------------------------------------------------ comandi
def queue(cmd):
    q = json.load(open(PENDING)) if os.path.exists(PENDING) else []
    q.append(cmd)
    json.dump(q, open(PENDING, "w"), ensure_ascii=False)

def push_cmds():
    _, cmd_chat, _ = cfg()
    q = json.load(open(PENDING)) if os.path.exists(PENDING) else []
    if not q:
        print("nessun comando in coda"); return
    pm = pinned(cmd_chat)
    seq = 1
    if pm and pm.get("text", "").startswith("{"):
        try: seq = json.loads(pm["text"])["seq"] + 1
        except Exception: pass
    msg = api("sendMessage", chat_id=cmd_chat, disable_notification=True,
              text=json.dumps({"seq": seq, "cmds": q}, ensure_ascii=False))
    api("pinChatMessage", chat_id=cmd_chat, message_id=msg["message_id"], disable_notification=True)
    os.remove(PENDING)
    print(f"inviati {len(q)} comandi (seq {seq})")

def apply_cmds(S):
    _, cmd_chat, _ = cfg()
    pm = pinned(cmd_chat)
    if not pm or not pm.get("text", "").startswith("{"):
        return 0
    pack = json.loads(pm["text"])
    if pack["seq"] <= S.get("cmd_seq", 0):
        return 0
    n = 0
    for c in pack["cmds"]:
        op = c["op"]
        if op == "order":
            rec = c["rec"]
            if all(o["id"] != rec["id"] for o in S["orders"]):
                rec["active_from"] = max(rec.get("active_from", 0), (e.now_ms() // 60000 + 1) * 60000)
                S["orders"].append(rec); n += 1
            S["next_id"] = max(S["next_id"], rec["id"] + 1)
        elif op == "cancel":
            for o in S["orders"]:
                if o["id"] == c["id"] and o["status"] == "pending":
                    o["status"], o["note"] = "cancelled", "annullato da Claude"; n += 1
        elif op == "modify":
            for p in S["positions"]:
                if p["id"] == c["id"]:
                    for k in ("stop", "tp1", "tp2"):
                        if c.get(k) is not None:
                            p[k] = c[k]
                    n += 1
        elif op == "cfg":
            S["cfg"][c["key"]] = c["value"]; n += 1
    S["cmd_seq"] = pack["seq"]
    e.save(S)
    e.send(f"🤖 Ricevuti {n} comandi da Claude (pacchetto {pack['seq']}).")
    return n

# ------------------------------------------------------------------ ciclo del server
def minute():
    S = e.load()
    n = apply_cmds(S)
    before = os.path.getsize(e.EVENTS) if os.path.exists(e.EVENTS) else 0
    e.cmd_tick(None)
    after = os.path.getsize(e.EVENTS) if os.path.exists(e.EVENTS) else 0
    m = dt.datetime.now(dt.timezone.utc).minute
    if m == 2:
        e.cmd_report(None)
    if n or after != before or m % 10 == 0:
        push_state()

def prepare():
    """Inizio di ogni controllo di Claude: se il server è attivo scarica il suo stato, altrimenti tick locale."""
    live, _ = server_live()
    if live:
        open(e.P("remote.flag"), "w").write("1")
        pull_state()
        print("MODALITÀ SERVER: il motore gira sul server; i tuoi place/cancel/modify vengono messi in coda.")
    else:
        if os.path.exists(e.P("remote.flag")): os.remove(e.P("remote.flag"))
        e.cmd_tick(None)
        print("MODALITÀ LOCALE: server non ancora attivo, tick eseguito qui.")

def finish():
    """Fine di ogni controllo: invia i comandi in coda al server (se attivo)."""
    if os.path.exists(e.P("remote.flag")):
        push_cmds()
    else:
        push_state()   # così il server, quando parte, riprende esattamente da qui

def bootstrap():
    """Solo sul server, una volta: prende l'ultimo stato pubblicato da Claude e diventa il motore ufficiale."""
    pull_state(any_origin=True)
    open(e.P("server.flag"), "w").write("1")
    if os.path.exists(e.P("remote.flag")): os.remove(e.P("remote.flag"))
    e.cmd_tick(None)
    push_state()
    e.send("🖥️ Il motore della sfida ora gira sul server: controllo ogni minuto, avvisi immediati.")

if __name__ == "__main__":
    {"pull": pull_state, "push": push_cmds, "minute": minute, "push-state": push_state,
     "prepare": prepare, "finish": finish, "bootstrap": bootstrap}[sys.argv[1]]()
