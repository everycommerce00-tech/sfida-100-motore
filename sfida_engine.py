#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motore di simulazione (paper trading) della "Sfida $100".

Comandi:
  init                 crea lo stato iniziale (una sola volta)
  tick                 ricostruisce tutto dalle candele 1m dall'ultimo controllo ad ora
  place --json '{...}' inserisce un ordine (limit/market), anche "ombra" (shadow)
  cancel <id>          annulla un ordine in attesa
  status               situazione attuale
  report               riepilogo + grafico (linee verdi = acquisti, rosse = vendite)

Dati: candele 1m e funding di OKX (BTC-USDT-SWAP). Tutto è ricostruibile: se il motore
non gira per ore, al tick successivo rigioca ogni minuto perso (stop/TP inclusi).
Ogni soglia è modificabile in state.json -> "cfg".
"""
import argparse, csv, datetime as dt, json, math, os, random, sys, time
import requests

try:
    from zoneinfo import ZoneInfo
    ROME = ZoneInfo("Europe/Rome")
except Exception:
    ROME = dt.timezone(dt.timedelta(hours=1))
UTC = dt.timezone.utc

BASE = os.path.dirname(os.path.abspath(__file__))
def P(n): return os.path.join(BASE, n)
STATE, TRADES, EQUITY, EVENTS, OUTBOX = (P(x) for x in
    ("state.json", "trades.csv", "equity.csv", "events.csv", "outbox.log"))
OKX = "https://www.okx.com/api/v5"
INST = "BTC-USDT-SWAP"
def inst_of(x): return x.get("inst") or INST
def short_name(i): return i.split("-")[0]

DEFAULT_CFG = dict(
    start_capital=100.0, max_lives=2, life_floor=20.0,
    fee_maker=0.0002, fee_taker=0.0005, slip_market=0.0001, slip_stop=0.0001, mmr=0.004,
    max_positions=1, min_rr_tp1=2.0, be_after_tp1=True,
    risk_ladder=[[150, 0.07], [400, 0.06], [1000, 0.04], [1e12, 0.03]],
    expense_prob=0.35, expense_min=0.02, expense_max=0.06,
    deadline_utc="2026-11-08 23:03", deadline_penalty=0.5, max_open_risk=0.25,
)

# Eventi macro (UTC) dal calendario 9-29 novembre 2026
MACRO = [("2026-11-09 08:00", "Scadenza Deribit settimanale"), ("2026-11-10 13:30", "CPI USA"),
         ("2026-11-13 08:00", "Scadenza Deribit settimanale"), ("2026-11-13 13:30", "PPI USA"),
         ("2026-11-17 13:30", "Vendite al dettaglio USA"), ("2026-11-18 19:00", "Verbali FOMC"),
         ("2026-11-20 08:00", "Scadenza Deribit settimanale"), ("2026-11-25 13:30", "PCE USA"),
         ("2026-11-27 08:00", "Scadenza Deribit mensile")]

EXPENSE_LABELS = ["fee di prelievo / bridge", "picco gas Solana (priority fee)",
                  "abbonamento dati (Coinglass)", "slippage anomalo su un fill",
                  "VPN / server", "commissione conversione stablecoin"]

# ---------------------------------------------------------------- utilità
def tsms(s): return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp() * 1000)
def iso(ms): return dt.datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")
def rome_s(ms): return dt.datetime.fromtimestamp(ms / 1000, UTC).astimezone(ROME).strftime("%d/%m %H:%M")
def now_ms(): return int(time.time() * 1000)
MACRO_MS = [(tsms(a), b) for a, b in MACRO]

def it(x, d=2):
    if d < 2 and x and 1 <= abs(x) < 100:
        d = 4
    if d < 2 and x and abs(x) < 1:
        s = f"{x:.{max(d, 4 - int(math.floor(math.log10(abs(x)))))}f}"
        return s.replace(".", ",")
    s = f"{x:,.{d}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")

def okx(path, params=None, tries=4):
    err = None
    for i in range(tries):
        try:
            j = requests.get(OKX + path, params=params, timeout=15).json()
            if j.get("code") == "0":
                return j["data"]
            err = j
        except Exception as e:
            err = str(e)
        time.sleep(1 + i)
    raise RuntimeError(f"OKX {path}: {err}")

def fetch_candles(start, end, inst=INST):
    """Candele 1m confermate con start <= ts < end, in ordine crescente."""
    out = {}
    need = max(1, (end - start) // 60000)
    for path, lim in (("/market/candles", 300), ("/market/history-candles", 100)):
        after = end
        while True:
            rows = okx(path, {"instId": inst, "bar": "1m", "after": after, "limit": lim})
            if not rows:
                break
            for r in rows:
                t = int(r[0])
                if start <= t < end and r[8] == "1":
                    out[t] = (t, float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            oldest = int(rows[-1][0])
            if oldest <= start:
                break
            after = oldest
            time.sleep(0.1)
        if len(out) >= need * 0.98:
            break
    return [out[k] for k in sorted(out)]

def fetch_funding(inst=INST):
    d = {}
    for r in okx("/public/funding-rate-history", {"instId": inst, "limit": "100"}):
        rate = r.get("realizedRate") or r.get("fundingRate")
        d[int(r["fundingTime"])] = float(rate)
    return d

def last_price(inst=INST):
    return float(okx("/market/ticker", {"instId": inst})[0]["last"])

def context(ts, funding, inst=INST):
    h = dt.datetime.fromtimestamp(ts / 1000, UTC).hour
    c = {"session": "Asia" if h < 7 else "Europa" if h < 13 else "NewYork" if h < 21 else "Notte"}
    nxt = [(m, l) for m, l in MACRO_MS if m > ts]
    if nxt:
        c["next_event"], c["hours_to_event"] = nxt[0][1], round((nxt[0][0] - ts) / 3.6e6, 1)
    fts = [k for k in funding if k <= ts]
    c["funding_pct"] = round(funding[max(fts)] * 100, 5) if fts else None
    try:
        rows = okx("/market/history-candles", {"instId": inst, "bar": "1H", "after": ts + 3600000, "limit": "30"})
        cl = [float(r[4]) for r in rows]
        c["chg24h_pct"] = round((cl[0] / cl[min(24, len(cl) - 1)] - 1) * 100, 2)
        c["atr1h_pct"] = round(sum(float(r[2]) - float(r[3]) for r in rows[:14]) / min(14, len(rows)) / cl[0] * 100, 3)
    except Exception:
        pass
    return c


CORR_FILE = P("corr.json")
CORR_NAMES = ["BTC", "ETH", "SOL", "DOGE", "XRP", "HYPE", "SUI", "PEPE", "WIF", "BNB"]

def corr_table():
    """Correlazioni dei rendimenti 15m (ultimi ~10 giorni), aggiornate al massimo una volta al giorno."""
    try:
        d = json.load(open(CORR_FILE))
        if time.time() - d["ts"] < 86400:
            return d["c"]
    except Exception:
        pass
    import numpy as np
    R = {}
    for n in CORR_NAMES:
        rows, after = [], None
        while len(rows) < 1000:
            p = {"instId": f"{n}-USDT-SWAP", "bar": "15m", "limit": "100"}
            if after: p["after"] = after
            r = okx("/market/history-candles", p)
            if not r: break
            rows += r; after = r[-1][0]
        rows = sorted(rows, key=lambda x: int(x[0]))
        R[n] = np.diff(np.log([float(x[4]) for x in rows]))
    m = min(len(v) for v in R.values())
    C = np.corrcoef(np.array([R[n][-m:] for n in CORR_NAMES]))
    c = {a: {b: round(float(C[i, j]), 3) for j, b in enumerate(CORR_NAMES)} for i, a in enumerate(CORR_NAMES)}
    json.dump({"ts": time.time(), "c": c}, open(CORR_FILE, "w"))
    return c

def rho(a, b, ct):
    a, b = short_name(a), short_name(b)
    if a == b: return 1.0
    return ct.get(a, {}).get(b, 0.75)

def portfolio_risk(items, ct, stress=False):
    """items: (inst, lato, rischio_usd). Rischio di portafoglio = sqrt(sum s_i s_j r_i r_j rho_ij)."""
    tot = 0.0
    for i1, s1, r1 in items:
        for i2, s2, r2 in items:
            sg = (1 if s1 == "long" else -1) * (1 if s2 == "long" else -1)
            c = rho(i1, i2, ct)
            if stress and i1 != i2:
                # stress: le posizioni nella stessa direzione si muovono quasi insieme (0,9),
                # le coperture (direzioni opposte) possono smettere di funzionare (0)
                c = max(c, 0.9) if sg > 0 else 0.0
            tot += sg * r1 * r2 * c
    return math.sqrt(max(tot, 0.0))

# ---------------------------------------------------------------- stato e log
def load():
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)

def save(S):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(S, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE)

def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)

def env():
    d = {}
    p = P(".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                d[k] = v
    d.update({k: v for k, v in os.environ.items() if k.startswith(("TELEGRAM_", "SFIDA_"))})
    return d

DASH_URL = "https://claude.ai/artifact/XTNJcW6iAXxEYn65KdohVS"

def send(text, photo=None, button=False):
    e, sent = env(), False
    tok, chat = e.get("TELEGRAM_BOT_TOKEN"), e.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            if photo:
                data = {"chat_id": chat, "caption": text[:1000]}
                if button:
                    data["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": "📈 Apri il monitor", "url": DASH_URL}]]})
                r = requests.post(f"https://api.telegram.org/bot{tok}/sendPhoto",
                                  data=data,
                                  files={"photo": open(photo, "rb")}, timeout=30)
            else:
                r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                                  data={"chat_id": chat, "text": text}, timeout=20)
            sent = r.ok
        except Exception:
            sent = False
    with open(OUTBOX, "a", encoding="utf-8") as f:
        f.write(f"[{dt.datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}Z] inviato={sent}\n{text}\n---\n")
    return sent

def event(S, ts, kind, text, notify=True):
    append_csv(EVENTS, ["ts_utc", "ts_roma", "tipo", "testo"], [iso(ts), rome_s(ts), kind, text.replace("\n", " | ")])
    if notify:
        send(text)

def cash_add(S, ts, amount, kind, note, shadow=False):
    if shadow:
        return
    S["cash"] = round(S["cash"] + amount, 6)
    S["ledger"].append({"ts": iso(ts), "tipo": kind, "importo": round(amount, 4), "nota": note,
                        "cassa": round(S["cash"], 4)})

def unreal(S, px=None):
    u = 0.0
    for p in S["positions"]:
        if not p["shadow"]:
            pp = S.get("last_px", {}).get(inst_of(p), px if px is not None else p["entry_px"])
            d = (p["entry_px"] - pp) if p["side"] == "short" else (pp - p["entry_px"])
            u += d * p["qty_open"]
    return u

def margin_used(S):
    return sum(p["margin"] * p["qty_open"] / p["qty_total"] for p in S["positions"] if not p["shadow"])

def risk_cap(S):
    for lim, pct in S["cfg"]["risk_ladder"]:
        if S["cash"] < lim:
            return pct
    return 0.03

# ---------------------------------------------------------------- spese a caso
def daily_expense(S, ts):
    cfg = S["cfg"]
    r = random.Random(f"sfida-edo-{S['day_no']}")
    if r.random() > cfg["expense_prob"]:
        return
    amt = max(1.0, round(S["cash"] * r.uniform(cfg["expense_min"], cfg["expense_max"]), 2))
    label = r.choice(EXPENSE_LABELS)
    cash_add(S, ts, -amt, "SPESA", f"giorno {S['day_no']}: {label}")
    event(S, ts, "SPESA", f"💸 Spesa a sorpresa (giorno {S['day_no']}): {label}, −${it(amt)}. Capitale: ${it(S['cash'])}")

# ---------------------------------------------------------------- posizioni
def trigger(o, c):
    ts, op, h, l, cl = c
    if ts < o["active_from"]:
        return None
    if o["type"] == "market":
        return op
    p = o["price"]
    if o["side"] == "short":
        return max(p, op) if h > p else None
    return min(p, op) if l < p else None

def open_pos(S, o, c, px, maker, funding):
    ts, cfg = c[0], S["cfg"]
    short, shadow = o["side"] == "short", o.get("shadow", False)
    if not shadow:
        why = None
        if sum(1 for p in S["positions"] if not p["shadow"]) >= cfg["max_positions"]:
            why = "già una posizione aperta (max 1)"
        elif o["qty"] * px / o["lev"] > S["cash"] - margin_used(S):
            why = "margine insufficiente"
        if why:
            o["status"], o["note"] = "cancelled", why
            event(S, ts, "ORDINE_ANNULLATO", f"⚠️ Ordine #{o['id']} annullato: {why}")
            return None
    fee = px * o["qty"] * (cfg["fee_maker"] if maker else cfg["fee_taker"])
    liq = px * (1 + 1 / o["lev"] - cfg["mmr"]) if short else px * (1 - 1 / o["lev"] + cfg["mmr"])
    pos = dict(id=o["id"], setup=o["setup"], reason=o.get("reason", ""), shadow=shadow, side=o["side"],
               qty_total=o["qty"], qty_open=o["qty"], lev=o["lev"], entry_ts=ts, entry_px=px,
               intended_px=o.get("price") or c[1], stop0=o["stop"], stop=o["stop"], tp1=o.get("tp1"),
               tp2=o.get("tp2"), tp1_frac=o.get("tp1_frac", 0.5), tp1_done=False,
               margin=px * o["qty"] / o["lev"], liq=liq, max_hold_ms=o.get("max_hold_ms"), pair=o.get("pair"), fees=fee, funding=0.0, gross=0.0,
               mae=0.0, mfe=0.0, exits=[], ctx=context(ts, funding, inst_of(o)), inst=inst_of(o))
    S["positions"].append(pos)
    o["status"] = "filled"
    if not shadow:
        S["first_trade_done"] = True
    cash_add(S, ts, -fee, "FEE", f"entrata #{o['id']}", shadow)
    S["fills"].append({"ts": ts, "side": "sell" if short else "buy", "px": px, "id": o["id"], "kind": "ENTRY", "inst": inst_of(o)})
    cfgf = S["cfg"]
    risk = (abs(px - o["stop"]) + px * cfgf["fee_taker"] + o["stop"] * (cfgf["fee_taker"] + cfgf["slip_stop"])) * o["qty"]
    tag = "👻 (ombra) " if shadow else ""
    gain = abs(o["tp1"] - px) * o["qty"] if o.get("tp1") else None
    event(S, ts, "ENTRATA",
          f"{'🔴' if short else '🟢'} {tag}APERTO {'SHORT (scommessa al ribasso)' if short else 'LONG (scommessa al rialzo)'} su {short_name(inst_of(o))} · #{o['id']}\n"
          f"Rischio massimo: −${it(risk)} ({it(risk / max(S['cash'], 1e-9) * 100, 1)}% del conto)"
          + (f" · obiettivo: +${it(gain)}" if gain else "") + "\n"
          f"Entrata {it(px, 1)} · stop {it(o['stop'], 0)}"
          + (f" · obiettivo {it(o['tp1'], 0)}" if o.get("tp1") else "") + (f" / {it(o['tp2'], 0)}" if o.get("tp2") else "")
          + f"\nIdea: {o['setup']} · leva {o['lev']}x · {rome_s(ts)}", notify=not shadow)
    return pos

def close_part(S, p, c, px, q, reason, maker):
    ts, cfg, short = c[0], S["cfg"], p["side"] == "short"
    gross = (p["entry_px"] - px) * q if short else (px - p["entry_px"]) * q
    fee = px * q * (cfg["fee_maker"] if maker else cfg["fee_taker"])
    p["qty_open"] -= q
    p["gross"] += gross
    p["fees"] += fee
    p["exits"].append({"ts": ts, "px": px, "qty": q, "reason": reason})
    cash_add(S, ts, gross - fee, "PNL", f"{reason} #{p['id']}", p["shadow"])
    S["fills"].append({"ts": ts, "side": "buy" if short else "sell", "px": px, "id": p["id"], "kind": reason, "inst": inst_of(p)})
    p["last_reason"] = reason
    tag = "👻 (ombra) " if p["shadow"] else ""
    if p["qty_open"] > 1e-9:
        event(S, ts, reason, f"✅ {tag}{short_name(inst_of(p))} #{p['id']}: primo obiettivo preso, incassati "
              f"{'+' if gross - fee >= 0 else '−'}${it(abs(gross - fee))}. Il resto resta aperto con lo stop all'entrata (rischio zero).",
              notify=not p["shadow"])
    if p["qty_open"] <= 1e-9:
        finalize(S, p, ts, reason)

def finalize(S, p, ts, reason):
    net = p["gross"] - p["fees"] + p["funding"]
    r0 = abs(p["entry_px"] - p["stop0"])
    risk_usd = r0 * p["qty_total"]
    hold = (ts - p["entry_ts"]) / 60000
    cx = p["ctx"]
    append_csv(TRADES,
        ["id", "mercato", "ombra", "setup", "motivo", "lato", "entrata_utc", "uscita_utc", "durata_min", "px_previsto", "px_entrata",
         "slippage_pct", "qty_btc", "nozionale", "leva", "margine", "stop_iniziale", "tp1", "tp2", "px_uscita_finale",
         "motivo_uscita", "lordo", "fee", "funding", "netto", "rischio_usd", "R", "MAE_px", "MFE_px", "MAE_R", "MFE_R",
         "sessione", "variazione24h_pct", "atr1h_pct", "funding_pct_entrata", "prossimo_evento", "ore_a_evento", "cassa_dopo"],
        [p["id"], short_name(inst_of(p)), int(p["shadow"]), p["setup"], p["reason"], p["side"], iso(p["entry_ts"]), iso(ts), round(hold, 1),
         p["intended_px"], round(p["entry_px"], 2), round((p["entry_px"] / p["intended_px"] - 1) * 100, 4),
         p["qty_total"], round(p["qty_total"] * p["entry_px"], 2), p["lev"], round(p["margin"], 2), p["stop0"],
         p["tp1"], p["tp2"], round(p["exits"][-1]["px"], 2), reason, round(p["gross"], 4), round(p["fees"], 4),
         round(p["funding"], 4), round(net, 4), round(risk_usd, 4), round(net / risk_usd, 3) if risk_usd else "",
         round(p["mae"], 1), round(p["mfe"], 1), round(p["mae"] / r0, 2) if r0 else "", round(p["mfe"] / r0, 2) if r0 else "",
         cx.get("session"), cx.get("chg24h_pct"), cx.get("atr1h_pct"), cx.get("funding_pct"), cx.get("next_event"),
         cx.get("hours_to_event"), round(S["cash"], 2)])
    S["positions"] = [x for x in S["positions"] if x is not p]
    S["closed"] = S.get("closed", 0) + (0 if p["shadow"] else 1)
    h, m = int(hold // 60), int(hold % 60)
    tag = "👻 (ombra) " if p["shadow"] else ""
    why = {"TP1": "obiettivo raggiunto 🎯", "TP2": "obiettivo finale raggiunto 🎯", "STOP": "stop toccato 🛑",
           "USCITA_TEMPO": "tempo massimo scaduto ⏱️", "LIQUIDAZIONE": "LIQUIDAZIONE 💥", "VITA_PERSA": "vita persa ☠️"}.get(reason, reason)
    ret = (S["cash"] / S["cfg"]["start_capital"] - 1) * 100
    event(S, ts, "TRADE_CHIUSO",
          f"{'✅' if net >= 0 else '❌'} {tag}CHIUSO {short_name(inst_of(p))} #{p['id']}: {why}\n"
          f"Risultato: {'+' if net >= 0 else '−'}${it(abs(net))} (commissioni e funding inclusi) · durata {h}h {m}m\n"
          f"Capitale: ${it(S['cash'])} ({'+' if ret >= 0 else ''}{it(ret, 1)}% dall'inizio)", notify=not p["shadow"])

def manage(S, p, c, entry_candle=False):
    ts, op, h, l, cl = c
    cfg, short = S["cfg"], p["side"] == "short"
    adv = (h - p["entry_px"]) if short else (p["entry_px"] - l)
    fav = (p["entry_px"] - l) if short else (h - p["entry_px"])
    p["mae"] = max(p["mae"], adv)
    if not entry_candle:
        p["mfe"] = max(p["mfe"], fav)
    hit_stop = h >= p["stop"] if short else l <= p["stop"]
    hit_liq = h >= p["liq"] if short else l <= p["liq"]
    liq_first = hit_liq and ((p["stop"] >= p["liq"]) if short else (p["stop"] <= p["liq"]))
    if liq_first:
        lost = p["margin"] * p["qty_open"] / p["qty_total"]
        q = p["qty_open"]
        p["qty_open"], p["gross"] = 0.0, p["gross"] - lost
        p["exits"].append({"ts": ts, "px": p["liq"], "qty": q, "reason": "LIQUIDAZIONE"})
        cash_add(S, ts, -lost, "LIQUIDAZIONE", f"#{p['id']}", p["shadow"])
        S["fills"].append({"ts": ts, "side": "buy" if short else "sell", "px": p["liq"], "id": p["id"], "kind": "LIQ", "inst": inst_of(p)})
        event(S, ts, "LIQUIDAZIONE", f"💥 LIQUIDAZIONE #{p['id']} @ {it(p['liq'], 1)} — margine perso −${it(lost)}", notify=not p["shadow"])
        finalize(S, p, ts, "LIQUIDAZIONE")
        return
    if hit_stop:
        gap = max(p["stop"], op) if short else min(p["stop"], op)
        px = gap * (1 + cfg["slip_stop"]) if short else gap * (1 - cfg["slip_stop"])
        close_part(S, p, c, px, p["qty_open"], "STOP", maker=False)
        return
    if entry_candle:
        return
    for k in ("tp1", "tp2"):
        tp = p.get(k)
        if not tp or p["qty_open"] <= 1e-9 or (k == "tp1" and p["tp1_done"]):
            continue
        if (l < tp) if short else (h > tp):
            if k == "tp1" and p.get("tp2"):
                q = min(p["qty_open"], p["qty_total"] * p["tp1_frac"])
            else:
                q = p["qty_open"]
            close_part(S, p, c, tp, q, "TP1" if k == "tp1" else "TP2", maker=True)
            if k == "tp1":
                p["tp1_done"] = True
                if cfg["be_after_tp1"] and p["qty_open"] > 1e-9:
                    p["stop"] = p["entry_px"]

def check_life(S, c):
    ts, cl = c[0], c[4]
    cfg = S["cfg"]
    if S["game_over"] or S["cash"] + unreal(S, cl) >= cfg["life_floor"]:
        return
    for p in list(S["positions"]):
        if not p["shadow"]:
            close_part(S, p, c, cl, p["qty_open"], "VITA_PERSA", maker=False)
    for o in S["orders"]:
        if o["status"] == "pending" and not o.get("shadow"):
            o["status"] = "cancelled"
    if S["life_no"] >= cfg["max_lives"]:
        S["game_over"] = True
        event(S, ts, "GAME_OVER", f"☠️ GAME OVER — anche la seconda vita è finita (capitale ${it(S['cash'])}).")
        return
    S["life_no"] += 1
    refill = cfg["start_capital"] - S["cash"]
    cash_add(S, ts, refill, "NUOVA_VITA", f"vita {S['life_no']}: capitale riportato a ${cfg['start_capital']:.0f}")
    S["first_trade_done"] = True
    event(S, ts, "VITA_PERSA", f"☠️ VITA PERSA. Parte la vita {S['life_no']}/{cfg['max_lives']} con ${it(cfg['start_capital'], 0)}. "
          f"Regola: rischio massimo 3–4% per trade, max 2–3 trade al giorno.")

def step(S, c, funding, inst=INST):
    ts, op, h, l, cl = c
    cfg = S["cfg"]
    S.setdefault("last_px", {})[inst] = cl
    day = dt.datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d")
    if day != S["cur_date"]:
        S["cur_date"] = day
        S["day_no"] += 1
        daily_expense(S, ts)
    if not S["first_trade_done"] and not S["penalty_applied"] and ts >= S["deadline_ms"]:
        loss = round(S["cash"] * cfg["deadline_penalty"], 2)
        cash_add(S, ts, -loss, "PENALITA", "nessun trade aperto entro la scadenza")
        S["penalty_applied"] = True
        event(S, ts, "PENALITA", f"⏰ Scadenza saltata: nessun trade aperto entro {rome_s(S['deadline_ms'])}. Penalità −${it(loss)}. Capitale: ${it(S['cash'])}")
    old = [p for p in S["positions"] if p["entry_ts"] < ts and inst_of(p) == inst]
    if ts in funding:
        for p in old:
            flow = funding[ts] * p["qty_open"] * op * (1 if p["side"] == "short" else -1)
            p["funding"] += flow
            cash_add(S, ts, flow, "FUNDING", f"#{p['id']} tasso {funding[ts] * 100:.4f}%", p["shadow"])
            event(S, ts, "FUNDING", f"funding #{p['id']}: {flow:+.4f}", notify=False)
    for p in old:
        manage(S, p, c)
    for p in [x for x in S["positions"] if x["entry_ts"] < ts and inst_of(x) == inst]:
        if p.get("max_hold_ms") and ts - p["entry_ts"] >= p["max_hold_ms"] and p["qty_open"] > 1e-9:
            close_part(S, p, c, cl, p["qty_open"], "USCITA_TEMPO", maker=False)
    for o in [x for x in S["orders"] if x["status"] == "pending" and inst_of(x) == inst]:
        if o.get("expire_ms") and ts >= o["expire_ms"]:
            o["status"] = "expired"
            continue
        px = trigger(o, c)
        if px is None:
            continue
        maker = o["type"] == "limit"
        if not maker:
            px = px * (1 - cfg["slip_market"]) if o["side"] == "short" else px * (1 + cfg["slip_market"])
        pos = open_pos(S, o, c, px, maker, funding)
        if pos:
            manage(S, pos, c, entry_candle=True)
    check_life(S, c)
    if dt.datetime.fromtimestamp(ts / 1000, UTC).minute == 0 and S.get("eq_ts") != ts:
        S["eq_ts"] = ts
        log_equity(S, ts, cl)

def log_equity(S, ts, px):
    u = unreal(S)
    append_csv(EQUITY, ["ts_utc", "cassa", "non_realizzato", "equity", "btc", "vita"],
               [iso(ts), round(S["cash"], 2), round(u, 2), round(S["cash"] + u, 2), px, S["life_no"]])

# ---------------------------------------------------------------- comandi
def cmd_init(a):
    if os.path.exists(STATE) and not a.force:
        sys.exit("state.json esiste già (usa --force per ricominciare)")
    cfg = dict(DEFAULT_CFG)
    t0 = tsms("2026-11-08 21:33")
    S = dict(cfg=cfg, cash=0.0, life_no=1, game_over=False, positions=[], orders=[], fills=[], ledger=[],
             last_ts=tsms("2026-11-08 21:58"), cur_date="2026-11-08", day_no=1, first_trade_done=False,
             penalty_applied=False, deadline_ms=tsms(cfg["deadline_utc"]), next_id=3, closed=0)
    cash_add(S, t0, 100.0, "DEPOSITO", "capitale iniziale")
    cash_add(S, tsms("2026-11-08 21:55"), -5.44, "SPESA", "giorno 1: abbonamento dati (Coinglass)")
    S["orders"].append(dict(id=1, status="cancelled", note="annullato: BTC crollato a 73.632 prima del fill", setup="retest", side="short",
                            type="limit", price=75500, qty=0.013, lev=15, stop=75950, tp1=74300, tp2=73300, active_from=t0))
    S["orders"].append(dict(id=2, status="pending", setup="Retest minimo rotto (dopo cascata di liquidazioni)",
                            reason="short sul ritorno al livello 74.850 (minimo 6/11), stop sopra, R:R 3 a TP1",
                            side="short", type="limit", price=74850.0, qty=0.012, lev=15, stop=75350.0, tp1=73350.0,
                            tp2=72600.0, tp1_frac=0.5, active_from=tsms("2026-11-08 21:59")))
    save(S)
    print("stato creato: cassa", S["cash"])

REMOTE = os.path.exists(P("remote.flag"))   # presente solo dove decide Claude, non sul server

def remote_queue(cmd):
    if REMOTE:
        import sync
        sync.queue(cmd)

def cmd_tick(a):
    if REMOTE and a is not None:
        print("modalità server: il motore gira sul server. Usa `python3 sync.py pull` per lo stato.")
        return
    S = load()
    if S["game_over"]:
        print("game over")
        return
    end = (now_ms() // 60000) * 60000
    start = S["last_ts"] + 60000
    if start >= end:
        print("niente di nuovo")
        return
    insts = {INST} | {inst_of(o) for o in S["orders"] if o["status"] == "pending"} | {inst_of(p) for p in S["positions"]}
    n_out0 = os.path.getsize(OUTBOX) if os.path.exists(OUTBOX) else 0
    seq = []
    fund = {}
    for i in insts:
        fund[i] = fetch_funding(i)
        for c in fetch_candles(start, end, i):
            seq.append((c[0], 0 if i == INST else 1, i, c))
    seq.sort(key=lambda x: (x[0], x[1]))
    for ts, _, i, c in seq:
        step(S, c, fund[i], i)
    candles = sorted({x[0] for x in seq})
    if candles:
        S["last_ts"] = candles[-1]
        log_equity(S, candles[-1], S["last_px"].get(INST, 0))
    save(S)
    print(f"processati {len(candles)} minuti ({len(insts)} mercati) fino a {iso(S['last_ts'])}Z · cassa ${S['cash']:.2f} · "
          f"posizioni {len(S['positions'])} · ordini in attesa {sum(1 for o in S['orders'] if o['status'] == 'pending')}")
    if os.path.exists(OUTBOX) and os.path.getsize(OUTBOX) > n_out0:
        with open(OUTBOX, encoding="utf-8") as f:
            f.seek(n_out0)
            print("--- nuovi messaggi:\n" + f.read())

def cmd_place(a):
    S = load()
    o = json.loads(a.json)
    cfg = S["cfg"]
    inst = o.get("inst", INST)
    last = last_price(inst)
    side, typ = o["side"], o.get("type", "limit")
    entry = o.get("price") or last
    stop, shadow = float(o["stop"]), bool(o.get("shadow", False))
    lev = int(o.get("lev", 10))
    if (side == "short" and stop <= entry) or (side == "long" and stop >= entry):
        sys.exit("stop dal lato sbagliato")
    per_btc = abs(entry - stop) + entry * (cfg["fee_maker"] + cfg["fee_taker"])
    if "qty" in o:
        qty = float(o["qty"])
    else:
        qty = math.floor(o["risk_pct"] * S["cash"] / per_btc * 10000) / 10000
    risk_pct = qty * per_btc / S["cash"]
    errs = []
    if not shadow and not o.get("force"):
        if risk_pct > risk_cap(S) + 1e-9:
            errs.append(f"rischio {risk_pct:.1%} sopra il tetto {risk_cap(S):.0%} (cfg.risk_ladder)")
        if o.get("tp1"):
            rr = abs(entry - float(o["tp1"])) / abs(entry - stop)
            if rr < cfg["min_rr_tp1"]:
                errs.append(f"R:R a TP1 {rr:.2f} < {cfg['min_rr_tp1']} (cfg.min_rr_tp1)")
        items = [(inst_of(x), x["side"], abs(x["entry_px"] - x["stop"]) * x["qty_open"]) for x in S["positions"] if not x["shadow"]]
        items += [(inst_of(x), x["side"], abs((x.get("price") or x.get("ref_px", last)) - x["stop"]) * x["qty"])
                  for x in S["orders"] if x["status"] == "pending" and not x.get("shadow")]
        items.append((inst, side, qty * per_btc))
        ct = corr_table()
        prisk = portfolio_risk(items, ct) / S["cash"]
        gross = portfolio_risk(items, ct, stress=True) / S["cash"]
        cap_p, cap_g = cfg.get("max_open_risk", 0.25), cfg.get("max_gross_risk", 0.50)
        cost_r = entry * (cfg["fee_maker"] if typ == "limit" else cfg["fee_taker"] + cfg["slip_market"]) + entry * (cfg["fee_taker"] + cfg["slip_stop"])
        cost_r /= abs(entry - stop)
        if cost_r > cfg.get("max_cost_r", 0.15) + 1e-9:
            errs.append(f"costo previsto {cost_r:.2f}R sopra {cfg.get('max_cost_r', 0.15)}R: allarga lo stop o entra limit (cfg.max_cost_r)")
        if prisk > cap_p + 1e-9:
            errs.append(f"rischio di portafoglio (corretto per correlazione) {prisk:.0%} sopra {cap_p:.0%} (cfg.max_open_risk)")
        if gross > cap_g + 1e-9:
            errs.append(f"rischio in stress {gross:.0%} sopra {cap_g:.0%} (cfg.max_gross_risk: correlazioni positive portate a 0,9, negative a 0)")
        print(f"rischio portafoglio {prisk:.1%} · in stress {gross:.1%} · costo {cost_r:.2f}R")
        if qty * entry / lev + sum(x["qty"] * (x.get("price") or x.get("ref_px", last)) / x["lev"] for x in S["orders"]
                                  if x["status"] == "pending" and not x.get("shadow")) + margin_used(S) > S["cash"]:
            errs.append("margine totale (posizioni + ordini in attesa) supera il capitale: alza la leva o riduci")
    if errs:
        sys.exit("ORDINE RIFIUTATO: " + "; ".join(errs))
    oid = S["next_id"]
    S["next_id"] += 1
    rec = dict(id=oid, inst=inst, ref_px=entry, status="pending", setup=o["setup"], reason=o.get("reason", ""), side=side, type=typ,
               price=o.get("price"), qty=qty, lev=lev, stop=stop, tp1=o.get("tp1"), tp2=o.get("tp2"),
               tp1_frac=o.get("tp1_frac", 0.5), shadow=shadow, active_from=(now_ms() // 60000 + 1) * 60000)
    if o.get("max_hold_h"):
        rec["max_hold_ms"] = int(o["max_hold_h"] * 3.6e6)
    if o.get("pair"):
        rec["pair"] = o["pair"]
    if o.get("expire_hours"):
        rec["expire_ms"] = now_ms() + int(o["expire_hours"] * 3.6e6)
    S["orders"].append(rec)
    save(S)
    remote_queue({"op": "order", "rec": rec})
    print(f"ordine #{oid} inserito: {side} {qty} {short_name(inst)} {typ} @ {entry} stop {stop} · rischio {risk_pct:.1%} (${risk_pct * S['cash']:.2f})"
          + (" [OMBRA]" if shadow else ""))

def cmd_cancel(a):
    S = load()
    for o in S["orders"]:
        if o["id"] == a.id and o["status"] == "pending":
            o["status"], o["note"] = "cancelled", "annullato a mano"
            save(S)
            remote_queue({"op": "cancel", "id": a.id})
            print("annullato", a.id)
            return
    print("ordine non trovato o non in attesa")

def status_text(S):
    px = last_price()
    S.setdefault("last_px", {}).update({inst_of(p): last_price(inst_of(p)) for p in S["positions"]})
    u = unreal(S, px)
    lines = [f"Capitale ${it(S['cash'])} (equity ${it(S['cash'] + u)}) · vita {S['life_no']}/{S['cfg']['max_lives']} · giorno {S['day_no']} · BTC {it(px, 1)}",
             f"Ultimo controllo: {rome_s(S['last_ts'])} · primo trade: {'fatto' if S['first_trade_done'] else 'DA FARE entro ' + rome_s(S['deadline_ms'])}"]
    for p in S["positions"]:
        px = last_price(inst_of(p))
        d = (p["entry_px"] - px) if p["side"] == "short" else (px - p["entry_px"])
        lines.append(f"POSIZIONE #{p['id']}{' (ombra)' if p['shadow'] else ''}: {p['side']} {p['qty_open']:.4f} {short_name(inst_of(p))} @ {it(p['entry_px'], 1)} · stop {it(p['stop'], 0)} · P/L {d * p['qty_open']:+.2f}")
    for o in S["orders"]:
        if o["status"] == "pending":
            lines.append(f"ORDINE #{o['id']}{' (ombra)' if o.get('shadow') else ''}: {o['side']} {o['type']} {o['qty']} {short_name(inst_of(o))} @ {o.get('price') or 'mercato'} · stop {o['stop']}")
    return "\n".join(lines)

def cmd_modify(a):
    S = load()
    for p in S["positions"]:
        if p["id"] == a.id:
            old = p["stop"]
            if a.stop is not None: p["stop"] = a.stop
            if a.tp1 is not None: p["tp1"] = a.tp1
            if a.tp2 is not None: p["tp2"] = a.tp2
            save(S)
            remote_queue({"op": "modify", "id": a.id, "stop": a.stop, "tp1": a.tp1, "tp2": a.tp2})
            event(S, now_ms(), "MODIFICA", f"🔧 #{p['id']} {short_name(inst_of(p))}: stop {it(old, 0)} → {it(p['stop'], 0)}"
                  + (f" · TP1 {it(p['tp1'], 0)}" if p.get("tp1") else ""))
            print("modificato", a.id)
            return
    print("posizione non trovata")

def cmd_status(a):
    print(status_text(load()))

def _equity_rows():
    if not os.path.exists(EQUITY):
        return []
    return [r for r in csv.DictReader(open(EQUITY, encoding="utf-8"))]

def perf_vs_btc(S):
    """Rendimento % del conto e di BTC (comprato e tenuto) dall'inizio della sfida."""
    rows = _equity_rows()
    btc0 = float(rows[0]["btc"]) if rows else None
    btc_now = last_price()
    eq = S["cash"] + unreal(S)
    p = (eq / S["cfg"]["start_capital"] - 1) * 100
    b = (btc_now / btc0 - 1) * 100 if btc0 else 0.0
    t0 = tsms("2026-11-08 21:33")
    target = (10 ** (((now_ms() - t0) / 86400000) / 21) - 1) * 100
    return p, b, target, eq

def f1(x):
    return f"{x:.1f}".replace(".", ",")

def sg(x):
    return ("+" if x >= 0 else "−") + f1(abs(x)) + "%"

def cmd_report(a, daily=False):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    S = load()
    T = lambda ms: dt.datetime.fromtimestamp(ms / 1000, UTC).astimezone(ROME)
    t0 = tsms("2026-11-08 21:33")
    # mercati da mostrare: SOLO posizioni aperte e ordini in attesa
    live = [("pos", p) for p in S["positions"] if not p["shadow"]] + \
           [("ord", o) for o in S["orders"] if o["status"] == "pending" and not o.get("shadow")]
    insts = []
    for _, x in live:
        if inst_of(x) not in insts:
            insts.append(inst_of(x))
    n = 1 + len(insts)
    fig, axes = plt.subplots(n, 1, figsize=(10, 3.4 + 2.6 * len(insts)), squeeze=False,
                             gridspec_kw={"height_ratios": [1.4] + [1] * len(insts)})
    axes = [x[0] for x in axes]
    # pannello 1: conto vs BTC vs obiettivo, in %
    rows = _equity_rows()
    if rows:
        rows = [{"ts_utc": "2026-11-08 21:33", "equity": S["cfg"]["start_capital"], "btc": rows[0]["btc"]}] + rows
        tt = [T(tsms(r["ts_utc"])) for r in rows]
        btc0 = float(rows[0]["btc"])
        pe = [(float(r["equity"]) / S["cfg"]["start_capital"] - 1) * 100 for r in rows]
        pb = [(float(r["btc"]) / btc0 - 1) * 100 for r in rows]
        po = [(10 ** (((tsms(r["ts_utc"]) - t0) / 86400000) / 21) - 1) * 100 for r in rows]
        ax = axes[0]
        ax.plot(tt, pe, color="#1565c0", lw=2.2, label=f"Sfida {pe[-1]:+.1f}%")
        ax.plot(tt, pb, color="#f7931a", lw=1.6, label=f"BTC tenuto {pb[-1]:+.1f}%")
        ax.plot(tt, po, color="#2e7d32", lw=1.2, ls="--", label=f"Percorso per ×10 {po[-1]:+.1f}%")
        ax.axhline(0, color="#999", lw=0.8)
        ax.set_ylabel("% dall'inizio")
        ax.legend(loc="upper left", fontsize=8)
        ax.set_title("La sfida contro BTC e contro l'obiettivo")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M", tz=ROME))
    # un pannello per ogni mercato con posizione o ordine attivo
    for ax, i in zip(axes[1:], insts):
        r = okx("/market/candles", {"instId": i, "bar": "15m", "limit": "64"})[::-1]
        ax.plot([T(int(x[0])) for x in r], [float(x[4]) for x in r], color="#222", lw=1)
        lab = []
        for kind, x in live:
            if inst_of(x) != i:
                continue
            if kind == "pos":
                ax.axhline(x["entry_px"], color="#1565c0", ls=":", lw=1.2)
                ax.axhline(x["stop"], color="red", ls="--", lw=1)
                if x.get("tp1"): ax.axhline(x["tp1"], color="green", ls="--", lw=1)
                if x.get("tp2"): ax.axhline(x["tp2"], color="green", ls=":", lw=1)
                ax.axvline(T(x["entry_ts"]), color="green" if x["side"] == "long" else "red", lw=1.4, alpha=0.8)
                px = float(r[-1][4])
                pl = ((x["entry_px"] - px) if x["side"] == "short" else (px - x["entry_px"])) * x["qty_open"]
                lab.append(f"#{x['id']} {x['side']} {pl:+.2f}$")
            else:
                ax.axhline(x["price"] or 0, color="orange", ls="--", lw=1.2)
                lab.append(f"ordine #{x['id']} {x['side']} @ {it(x['price'] or 0, 1)}")
        ax.set_title(f"{short_name(i)} (15m) — " + " · ".join(lab), fontsize=9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=ROME))
    fig.tight_layout()
    fig.savefig(P("report.png"), dpi=110)
    plt.close(fig)
    p, b, target, eq = perf_vs_btc(S)
    closed = list(csv.DictReader(open(TRADES, encoding="utf-8"))) if os.path.exists(TRADES) else []
    real = [r for r in closed if r["ombra"] == "0"]
    today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
    td = [r for r in real if r["uscita_utc"].startswith(today)]
    lines = [("📅 RIEPILOGO DEL GIORNO" if daily else "📈 AGGIORNAMENTO") + " — SFIDA $100",
             f"Conto: ${it(eq)} ({sg(p)}) · BTC tenuto: {sg(b)} · {'meglio' if p >= b else 'peggio'} di BTC di {f1(abs(p - b))} punti",
             f"Obiettivo ×10: oggi dovrei essere a {sg(target)} → {'avanti' if p >= target else 'indietro'} di {f1(abs(p - target))} punti",
             f"Vita {S['life_no']}/{S['cfg']['max_lives']} · giorno {S['day_no']} di 21"]
    if td:
        lines.append(f"Oggi: {len(td)} trade chiusi, {sum(1 for r in td if float(r['netto']) > 0)} vinti, "
                     f"{'+' if sum(float(r['netto']) for r in td) >= 0 else '−'}${it(abs(sum(float(r['netto']) for r in td)))}")
    if daily and real:
        by = {}
        for r in real:
            import re
            k = re.sub(r" [A-Z]+/[A-Z]+", "", r["setup"].split("(")[0]).replace("Forza relativa", "Coppia forza relativa").strip()
            v = by.setdefault(k, [0, 0, 0.0]); v[0] += 1; v[1] += float(r["netto"]) > 0; v[2] += float(r["netto"])
        lines.append("Per tipo di idea (trade · vinti · netto):")
        for k, v in sorted(by.items(), key=lambda kv: kv[1][2]):
            lines.append(f"  {k}: {v[0]} · {v[1]} · {'+' if v[2] >= 0 else '−'}${it(abs(v[2]))}")
        fees = sum(float(r["fee"]) for r in real)
        lines.append(f"Commissioni pagate finora: ${it(fees)}")
    if not live:
        lines.append("Nessuna posizione aperta né ordine in attesa.")
    txt = "\n".join(lines)
    send(txt, photo=P("report.png"), button=True)
    print(txt)

def cmd_daily(a):
    cmd_report(a, daily=True)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    i = sp.add_parser("init"); i.add_argument("--force", action="store_true"); i.set_defaults(f=cmd_init)
    sp.add_parser("tick").set_defaults(f=cmd_tick)
    pl = sp.add_parser("place"); pl.add_argument("--json", required=True); pl.set_defaults(f=cmd_place)
    ca = sp.add_parser("cancel"); ca.add_argument("id", type=int); ca.set_defaults(f=cmd_cancel)
    mo = sp.add_parser("modify"); mo.add_argument("id", type=int); mo.add_argument("--stop", type=float)
    mo.add_argument("--tp1", type=float); mo.add_argument("--tp2", type=float); mo.set_defaults(f=cmd_modify)
    sp.add_parser("status").set_defaults(f=cmd_status)
    sp.add_parser("report").set_defaults(f=cmd_report)
    sp.add_parser("daily").set_defaults(f=cmd_daily)
    a = ap.parse_args()
    a.f(a)

if __name__ == "__main__":
    main()
