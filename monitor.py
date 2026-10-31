#!/usr/bin/env python3
"""Indonesia Flight Deal Monitor.

Ressourcenschonender Flugpreis-Monitor ohne KI-Aufrufe:
  * Preisdaten: SerpApi Google Flights API (Free Plan 250 Suchen/Monat)
  * Speicherung: JSON-Dateien in ./data (werden vom GitHub-Workflow committet)
  * Benachrichtigung: Discord (Webhook oder Bot), optional Telegram/E-Mail
  * Dashboard: statische HTML-Seite ./docs/index.html (GitHub Pages)

Nur Python-Standardbibliothek (>= 3.11). Befehle:
  python monitor.py run                # ein Suchlauf (Standard)
  python monitor.py dashboard          # nur Dashboard neu erzeugen
  python monitor.py test-notify        # Testnachricht an alle Kanäle
  python monitor.py telegram-chat-id   # Chat-ID des Telegram-Bots ermitteln (optional)
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import os
import re
import smtplib
import ssl
import statistics
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("FDM_DATA_DIR", ROOT / "data"))
DOCS = Path(os.environ.get("FDM_DOCS_DIR", ROOT / "docs"))
SERPAPI_URL = "https://serpapi.com/search.json"
SERPAPI_ACCOUNT_URL = "https://serpapi.com/account.json"
SELF_TRANSFER_RE = re.compile(r"self[- ]?transfer|separate tickets?|getrennte tickets?|selbst umsteigen", re.I)
HTTP_TIMEOUT = 75


def now_utc() -> dt.datetime:
    override = os.environ.get("FDM_NOW")  # nur für Tests
    if override:
        return dt.datetime.fromisoformat(override)
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- config / storage
def load_config(path: Path | None = None) -> dict:
    with open(path or Path(os.environ.get("FDM_CONFIG", ROOT / "config.toml")), "rb") as f:
        return tomllib.load(f)


def read_json(name: str, default):
    p = DATA / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(name: str, obj) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / (name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(DATA / name)


def read_observations() -> list[dict]:
    p = DATA / "observations.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text("utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def append_observations(rows: list[dict]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "observations.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- search plan
def all_combos(cfg: dict, today: dt.date) -> dict[str, list[dict]]:
    """Alle (Variante, Abflug, Rückflug)-Kombinationen, je Variante gespreizt sortiert."""
    t = cfg["trip"]
    start = dt.date.fromisoformat(t["depart_from"])
    end = dt.date.fromisoformat(t["depart_to"])
    step = int(t.get("depart_step_days", 4))
    durations = t["durations"]
    dates = []
    d, i = start, 0
    while d <= end:
        if d > today:
            dates.append((d, durations[i % len(durations)]))
        d += dt.timedelta(days=step)
        i += 1
    n = len(dates)
    # Spreizung: Reihenfolge i*stride mod n, damit aufeinanderfolgende Suchen weit auseinander liegen
    stride = next((s for s in (7, 5, 3, 11, 13, 1) if n and s < max(n, 2) and _gcd(s, n) == 1), 1)
    order = [(k * stride) % n for k in range(n)] if n else []
    plans = {}
    for v in cfg["variants"]:
        plans[v["id"]] = [
            {"variant": v["id"], "out": dates[k][0].isoformat(),
             "ret": (dates[k][0] + dt.timedelta(days=dates[k][1])).isoformat()}
            for k in order
        ]
    return plans


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def weighted_pattern(variants: list[dict]) -> list[str]:
    """Glatte gewichtete Round-Robin-Reihenfolge (z. B. V1 V2 V1 V3 V1 V2 V4 V3)."""
    weights = {v["id"]: int(v.get("weight", 1)) for v in variants if int(v.get("weight", 1)) > 0}
    total = sum(weights.values())
    cur = {k: 0 for k in weights}
    out = []
    for _ in range(total):
        for k in cur:
            cur[k] += weights[k]
        best = max(cur, key=cur.get)
        cur[best] -= total
        out.append(best)
    return out


def next_combos(cfg: dict, state: dict, k: int, today: dt.date, recheck: dict | None) -> list[dict]:
    plans = all_combos(cfg, today)
    pattern = weighted_pattern(cfg["variants"])
    if not pattern or not any(plans.values()):
        return []
    picked: list[dict] = []
    if recheck and k >= 2:
        picked.append({**recheck, "recheck": True})
    cur = state.setdefault("cursor", {"pattern": 0, "variant": {}})
    guard = 0
    while len(picked) < k and guard < 10 * len(pattern):
        guard += 1
        vid = pattern[cur["pattern"] % len(pattern)]
        cur["pattern"] = (cur["pattern"] + 1) % len(pattern)
        lst = plans.get(vid) or []
        if not lst:
            continue
        idx = cur["variant"].get(vid, 0) % len(lst)
        cur["variant"][vid] = (idx + 1) % len(lst)
        c = lst[idx]
        if any(p["variant"] == c["variant"] and p["out"] == c["out"] and p["ret"] == c["ret"] for p in picked):
            continue
        picked.append(c)
    return picked


# --------------------------------------------------------------------------- budget
def searches_for_run(cfg: dict, state: dict, now: dt.datetime, left: int | None) -> tuple[int, int, str]:
    """Liefert (Suchen, max. Verifikationen, Hinweis)."""
    b = cfg["budget"]
    month = now.strftime("%Y-%m")
    used = state.get("usage", {}).get(month, 0)
    if left is None:
        left = max(0, int(b["monthly_searches"]) - used)
        src = "eigene Zählung"
    else:
        src = "SerpApi-Konto"
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left = days_in_month - now.day + 1
    runs_left = max(1, days_left * int(b["runs_per_day"]) - (1 if now.hour >= 12 else 0))
    reserve = int(b["reserve_for_verification"])
    reserve_now = min(reserve, max(0, round(reserve * days_left / days_in_month)))
    spendable = left - reserve_now
    if left <= 0:
        return 0, 0, f"Suchbudget erschöpft ({src})"
    per_run = spendable // runs_left if spendable > 0 else 0
    # Rest aus Rundung im Monat nutzen: bei Überschuss mindestens 1 Suche
    if per_run == 0 and spendable > 0:
        per_run = 1
    per_run = min(int(b["max_searches_per_run"]), per_run)
    ver = min(int(b["max_verifications_per_run"]), max(0, left - per_run) // 2)
    return per_run, ver, f"{left} Suchen übrig ({src}), {runs_left} Läufe bis Monatsende"


# --------------------------------------------------------------------------- SerpApi
class SourceError(Exception):
    pass


def http_get_json(url: str, params: dict, retries: int = 1) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "indonesia-flight-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            try:
                msg = json.loads(body).get("error", body)
            except json.JSONDecodeError:
                msg = body
            last = SourceError(f"HTTP {e.code}: {msg}")
            if e.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = SourceError(f"Quelle nicht erreichbar: {e}")
        except json.JSONDecodeError:
            last = SourceError("Antwort war kein gültiges JSON")
    raise last  # type: ignore[misc]


def serpapi_account(key: str) -> int | None:
    """Kostenloser Abruf (zählt nicht als Suche). None, wenn nicht ermittelbar."""
    try:
        acc = http_get_json(SERPAPI_ACCOUNT_URL, {"api_key": key}, retries=0)
        v = acc.get("total_searches_left", acc.get("plan_searches_left"))
        return int(v) if v is not None else None
    except (SourceError, ValueError, TypeError):
        return None


def base_params(cfg: dict, combo: dict, key: str) -> dict:
    v = variant_by_id(cfg, combo["variant"])
    t = cfg["trip"]
    p = {
        "engine": "google_flights", "api_key": key,
        "currency": t.get("currency", "EUR"), "gl": cfg["serpapi"]["gl"], "hl": cfg["serpapi"]["hl"],
        "adults": t.get("adults", 1), "travel_class": 1,
        "stops": {0: 1, 1: 2, 2: 3}.get(int(t.get("max_stops", 1)), 0),
    }
    if v["outbound_to"] == v["return_from"]:
        p.update(type=1, departure_id=t["origin"], arrival_id=v["outbound_to"],
                 outbound_date=combo["out"], return_date=combo["ret"])
    else:
        p.update(type=3, multi_city_json=json.dumps([
            {"departure_id": t["origin"], "arrival_id": v["outbound_to"], "date": combo["out"]},
            {"departure_id": v["return_from"], "arrival_id": t["origin"], "date": combo["ret"]},
        ], separators=(",", ":")))
    return p


def serpapi_search(params: dict) -> dict:
    data = http_get_json(SERPAPI_URL, params)
    if data.get("error"):
        err = str(data["error"])
        if "hasn't returned any results" in err or "no results" in err.lower():
            return {"_no_results": True, "_error": err}
        raise SourceError(err)
    status = (data.get("search_metadata") or {}).get("status")
    if status and status not in ("Success", "Cached"):
        raise SourceError(f"SerpApi-Status: {status}")
    return data


# --------------------------------------------------------------------------- parsing
def variant_by_id(cfg: dict, vid: str) -> dict:
    return next(v for v in cfg["variants"] if v["id"] == vid)


def parse_leg(it: dict) -> dict:
    segs = []
    for s in it.get("flights") or []:
        segs.append({
            "airline": s.get("airline", "?"),
            "flight_number": s.get("flight_number", ""),
            "from": (s.get("departure_airport") or {}).get("id", "?"),
            "dep": (s.get("departure_airport") or {}).get("time", ""),
            "to": (s.get("arrival_airport") or {}).get("id", "?"),
            "arr": (s.get("arrival_airport") or {}).get("time", ""),
            "duration_min": s.get("duration"),
            "overnight": bool(s.get("overnight")),
        })
    lays = [{"airport": l.get("id", "?"), "duration_min": l.get("duration") or 0,
             "overnight": bool(l.get("overnight"))} for l in it.get("layovers") or []]
    blob = json.dumps({k: it.get(k) for k in ("extensions", "flights", "layovers", "type")}, ensure_ascii=False)
    return {
        "segments": segs,
        "layovers": lays,
        "stops": max(0, len(segs) - 1),
        "duration_min": it.get("total_duration"),
        "airlines": sorted({s["airline"] for s in segs}),
        "self_transfer": bool(SELF_TRANSFER_RE.search(blob)),
    }


def parse_itineraries(data: dict) -> list[dict]:
    out = []
    for group in ("best_flights", "other_flights"):
        for it in data.get(group) or []:
            price = it.get("price")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            out.append({
                "price": float(price),
                "leg": parse_leg(it),
                "departure_token": it.get("departure_token"),
                "booking_token": it.get("booking_token"),
            })
    out.sort(key=lambda o: o["price"])
    return out


def leg_ok(cfg: dict, leg: dict | None) -> tuple[bool, list[str]]:
    if not leg:
        return True, []
    t = cfg["trip"]
    issues = []
    if leg["stops"] > int(t.get("max_stops", 1)):
        issues.append(f"{leg['stops']} Umstiege")
    if leg.get("duration_min") and leg["duration_min"] > t.get("max_leg_duration_h", 26) * 60:
        issues.append(f"lange Reisezeit ({fmt_dur(leg['duration_min'])})")
    for l in leg["layovers"]:
        if l["duration_min"] > t.get("max_layover_h", 8) * 60:
            issues.append(f"langer Umstieg {l['airport']} ({fmt_dur(l['duration_min'])})")
        if l["overnight"]:
            issues.append(f"Umstieg über Nacht in {l['airport']}")
    if leg["self_transfer"]:
        issues.append("Self-Transfer / getrennte Tickets")
    return not issues, issues


def baggage_estimate(cfg: dict, airlines: list[str]) -> tuple[float, str]:
    inc = {a.lower() for a in cfg["baggage"]["usually_included"]}
    if airlines and all(a.lower() in inc for a in airlines):
        return 0.0, "wahrscheinlich inklusive (Airline-Standard, nicht verifiziert)"
    return float(cfg["baggage"]["surcharge_unknown_eur"]), \
        f"unbekannt – {cfg['baggage']['surcharge_unknown_eur']} € Zuschlag angesetzt"


def fmt_dur(m) -> str:
    if not m:
        return "?"
    return f"{int(m) // 60} h {int(m) % 60:02d} min"


# --------------------------------------------------------------------------- evaluation
def tier_for(cfg: dict, eff: float) -> str:
    th = cfg["thresholds"]
    if eff < th["extreme"]:
        return "EXTREM GUT"
    if eff < th["good"]:
        return "GUT"
    if eff < th["normal"]:
        return "NORMAL"
    return "ZU TEUER"


def baseline(cfg: dict, obs: list[dict], vid: str, now: dt.datetime) -> float | None:
    th = cfg["thresholds"]
    cutoff = now - dt.timedelta(days=int(th["baseline_days"]))
    vals = [o["best_total"] for o in obs
            if o.get("variant") == vid and o.get("status") == "ok" and o.get("best_total")
            and parse_iso(o["ts"]) >= cutoff]
    if len(vals) < int(th["baseline_min_samples"]):
        return None
    return float(statistics.median(vals))


def evaluate(cfg: dict, offer: dict, vid: str, base: float | None) -> dict:
    v = variant_by_id(cfg, vid)
    bag_cost, bag_note = offer.get("bag_cost"), offer.get("bag_note")
    if bag_cost is None:
        bag_cost, bag_note = baggage_estimate(cfg, offer["leg"]["airlines"])
    total = offer["price"] + bag_cost
    eff = total + float(v.get("penalty_eur", 0))
    tier = tier_for(cfg, eff)
    ok_out, issues = leg_ok(cfg, offer["leg"])
    ok_ret, issues_r = leg_ok(cfg, offer.get("return_leg"))
    issues += [f"Rückflug: {i}" for i in issues_r]
    good_conn = ok_out and ok_ret
    pct_below = round((1 - total / base) * 100, 1) if base else None
    reasons = []
    th = cfg["thresholds"]
    self_transfer = offer["leg"]["self_transfer"] or bool((offer.get("return_leg") or {}).get("self_transfer")) \
        or bool(offer.get("separate_tickets"))
    if tier == "EXTREM GUT":
        reasons.append(f"unter {th['extreme']} €")
    if tier == "GUT" and good_conn:
        reasons.append(f"{th['extreme']}–{th['good']} € mit guter Verbindung")
    if pct_below is not None and pct_below >= th["below_baseline_pct"] and eff < th["normal"]:
        reasons.append(f"{pct_below:.0f} % unter beobachtetem Niveau (Median {base:.0f} €)")
    notify = bool(reasons) and not self_transfer
    if self_transfer and reasons:
        issues.append("nicht gemeldet: Self-Transfer/getrennte Tickets")
    return {"total": round(total, 2), "effective": round(eff, 2), "bag_cost": bag_cost, "bag_note": bag_note,
            "tier": tier, "good_connection": good_conn, "issues": issues, "pct_below_baseline": pct_below,
            "baseline": base, "reasons": reasons, "notify": notify}


# --------------------------------------------------------------------------- verification
BAG_FREE_RE = re.compile(r"(1st|first|1)\s*checked bag[^,;]*?(free|included|inklusive|0\s*€|€\s*0\b)|"
                         r"(free|included)\s*(1st\s*)?checked bag|checked bag[s]? included", re.I)
BAG_PRICE_RE = re.compile(r"(1st|first)?\s*checked bag[^0-9€]*?(?:€\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*(?:€|EUR))",
                          re.I)


def parse_booking_options(data: dict) -> dict:
    """Wertet SerpApi booking_options aus: günstigste Option, Gepäck, getrennte Tickets."""
    opts = data.get("booking_options") or []
    best, separate = None, False
    for o in opts:
        tog = o.get("together")
        if not tog:
            if o.get("departing") or o.get("returning") or o.get("separate_tickets"):
                separate = True
            continue
        price = tog.get("price")
        if not isinstance(price, (int, float)):
            continue
        if best is None or price < best["price"]:
            best = {"price": float(price), "book_with": tog.get("book_with", ""),
                    "option_title": tog.get("option_title", ""),
                    "baggage": [str(x) for x in (tog.get("baggage_prices") or [])],
                    "extensions": [str(x) for x in (tog.get("extensions") or [])]}
    if best is None:
        return {"separate_tickets": separate, "found": False}
    text = " | ".join(best["baggage"] + best["extensions"])
    bag_cost, bag_note = None, None
    if BAG_FREE_RE.search(text):
        bag_cost, bag_note = 0.0, "inklusive (laut Buchungsoption verifiziert)"
    else:
        m = BAG_PRICE_RE.search(text)
        if m:
            per = float((m.group(2) or m.group(3)).replace(",", "."))
            bag_cost, bag_note = per * 2, f"{per:.0f} € pro Strecke laut Anbieter (×2 angesetzt)"
        elif re.search(r"checked bag", text, re.I) and re.search(r"not included|no checked|0 checked", text, re.I):
            bag_cost, bag_note = None, "nicht inklusive, Preis unbekannt"
    return {"found": True, "separate_tickets": False, "split_options_seen": separate, **best, "bag_cost": bag_cost,
            "bag_note": bag_note, "baggage_text": text}


def verify(cfg: dict, params: dict, offer: dict, budget: list[int]) -> dict:
    """Holt Rückflug + Buchungsoptionen (max. 2 zusätzliche Suchen). Ändert offer in-place."""
    notes = []
    tok = offer.get("departure_token")
    if tok and budget[0] > 0:
        budget[0] -= 1
        data = serpapi_search({**params, "departure_token": tok})
        rets = parse_itineraries(data)
        valid = [r for r in rets if leg_ok(cfg, r["leg"])[0]] or rets
        if valid:
            r = valid[0]
            offer["return_leg"] = r["leg"]
            offer["price"] = r["price"]
            offer["booking_token"] = r.get("booking_token")
            offer["google_flights_url"] = (data.get("search_metadata") or {}).get("google_flights_url") \
                or offer.get("google_flights_url")
        else:
            notes.append("Rückflug-Optionen nicht abrufbar")
    elif tok:
        notes.append("Budget: Rückflug nicht verifiziert")
    btok = offer.get("booking_token")
    if btok and budget[0] > 0:
        budget[0] -= 1
        bp = {k: params[k] for k in ("engine", "api_key", "currency", "gl", "hl")}
        data = serpapi_search({**bp, "booking_token": btok})
        b = parse_booking_options(data)
        offer["booking"] = b
        if b.get("found"):
            offer["price"] = b["price"]
            if b.get("bag_cost") is not None:
                offer["bag_cost"], offer["bag_note"] = b["bag_cost"], b["bag_note"]
            elif b.get("bag_note"):  # laut Anbieter nicht inklusive, Preis unbekannt -> Pauschale ansetzen
                sc = float(cfg["baggage"]["surcharge_unknown_eur"])
                offer["bag_cost"], offer["bag_note"] = sc, f"laut Anbieter NICHT inklusive – {sc:.0f} € Pauschale angesetzt"
        if b.get("separate_tickets") and not b.get("found"):
            offer["separate_tickets"] = True
    elif btok:
        notes.append("Budget: Buchungsoptionen nicht abgefragt")
    else:
        notes.append("keine Buchungsoptionen verfügbar")
    offer["verified"] = "return_leg" in offer
    offer["verify_notes"] = notes
    return offer


# --------------------------------------------------------------------------- notifications
def deal_key(d: dict) -> str:
    segs = d["offer"]["leg"]["segments"]
    raw = "|".join([d["variant"], d["out"], d["ret"], ",".join(d["offer"]["leg"]["airlines"]),
                    ",".join(s["flight_number"] for s in segs)])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


TIER_ORDER = ["ZU TEUER", "NORMAL", "GUT", "EXTREM GUT"]


def should_notify(cfg: dict, state: dict, d: dict, now: dt.datetime | None = None) -> bool:
    """Doppelte Meldungen vermeiden.

    * gleicher Deal (Variante, Daten, Flüge): nur bei höherer Stufe oder >= renotify_drop_eur günstiger
    * andere Termine derselben Variante: nur wenn günstiger als die beste Meldung der letzten 7 Tage
      (minus renotify_drop_eur) oder höhere Stufe
    """
    drop = float(cfg["thresholds"]["renotify_drop_eur"])
    tier, total = TIER_ORDER.index(d["eval"]["tier"]), d["eval"]["total"]
    notified = state.get("notified", {})
    prev = notified.get(d["key"])
    if prev:
        return tier > TIER_ORDER.index(prev.get("tier", "ZU TEUER")) or total <= prev["total"] - drop
    now = now or parse_iso(d["ts"])
    recent = [x for x in notified.values() if x.get("variant") == d["variant"]
              and now - parse_iso(x["ts"]) <= dt.timedelta(days=7)]
    if not recent:
        return True
    best = min(recent, key=lambda x: x["total"])
    best_tier = max(TIER_ORDER.index(x.get("tier", "ZU TEUER")) for x in recent)
    return tier > best_tier or total <= best["total"] - drop


def leg_lines(leg: dict | None, title: str) -> list[str]:
    if not leg:
        return [f"<b>{title}:</b> Details nicht abgefragt (nur Gesamtpreis bekannt)"]
    segs = leg["segments"]
    if not segs:
        return [f"<b>{title}:</b> keine Segmentdaten"]
    stops = "Nonstop" if leg["stops"] == 0 else f"{leg['stops']} Umstieg" + ("e" if leg["stops"] > 1 else "")
    lay = ", ".join(f"{l['airport']} {fmt_dur(l['duration_min'])}" for l in leg["layovers"])
    out = [f"<b>{title}:</b> {escape(segs[0]['dep'])} {segs[0]['from']} → {escape(segs[-1]['arr'])} {segs[-1]['to']}",
           f"   {fmt_dur(leg['duration_min'])}, {stops}" + (f" ({escape(lay)})" if lay else "")]
    out.append("   " + " · ".join(escape(f"{s['flight_number']} {s['from']}-{s['to']}") for s in segs))
    return out


def format_deal(cfg: dict, d: dict) -> str:
    e, o = d["eval"], d["offer"]
    v = variant_by_id(cfg, d["variant"])
    icon = {"EXTREM GUT": "🔥", "GUT": "✅"}.get(e["tier"], "📉")
    lines = [f"{icon} <b>{e['tier']}: {e['total']:.0f} € gesamt</b>",
             f"{escape(v['name'])}",
             f"📅 {d['out']} → {d['ret']} ({(dt.date.fromisoformat(d['ret']) - dt.date.fromisoformat(d['out'])).days} Tage)",
             f"✈️ {escape(', '.join(o['leg']['airlines']))}"]
    lines += leg_lines(o["leg"], "Hinflug")
    lines += leg_lines(o.get("return_leg"), "Rückflug")
    lines.append(f"🧳 Gepäck: {escape(e['bag_note'])}")
    lines.append(f"💶 Ticket {o['price']:.0f} € + Gepäck {e['bag_cost']:.0f} € = <b>{e['total']:.0f} €</b>"
                 + (f" (Bewertung inkl. {v['penalty_eur']} € Varianten-Aufschlag: {e['effective']:.0f} €)"
                    if v.get("penalty_eur") else ""))
    lines.append("Grund: " + escape("; ".join(e["reasons"])))
    if e["issues"]:
        lines.append("⚠️ " + escape("; ".join(e["issues"])))
    for n in o.get("verify_notes") or []:
        lines.append("ℹ️ " + escape(n))
    b = o.get("booking") or {}
    if b.get("book_with"):
        lines.append(f"🏷️ Buchbar bei: {escape(b['book_with'])}" + (f" ({escape(b['option_title'])})" if b.get("option_title") else ""))
    if o.get("google_flights_url"):
        lines.append(f'🔗 <a href="{escape(o["google_flights_url"], quote=True)}">Zu Google Flights (Buchungsoptionen)</a>')
    dash = cfg["notify"].get("dashboard_url")
    if dash:
        lines.append(f'📊 <a href="{escape(dash, quote=True)}">Dashboard</a>')
    lines.append("<i>Preise können sich jederzeit ändern – vor Buchung prüfen.</i>")
    return "\n".join(lines)


def html_to_text(s: str) -> str:
    s = re.sub(r'<a href="([^"]+)">([^<]+)</a>', lambda m: f"{m.group(2)}: {m.group(1).replace('&amp;', '&')}", s)
    s = re.sub(r"</?[bi]>", "", s)
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#x27;", "'")


def channels() -> list[str]:
    ch = []
    if os.environ.get("DISCORD_WEBHOOK_URL") or (os.environ.get("DISCORD_BOT_TOKEN") and (
            os.environ.get("DISCORD_CHANNEL_ID") or os.environ.get("DISCORD_USER_ID"))):
        ch.append("discord")
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        ch.append("telegram")
    if all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO")):
        ch.append("email")
    return ch


# ---- Discord ------------------------------------------------------------------------------
DISCORD_API = "https://discord.com/api/v10"
DISCORD_UA = "DiscordBot (https://github.com/indonesia-flight-monitor, 1.0)"
TIER_COLOR = {"EXTREM GUT": 0x0CA30C, "GUT": 0x2A78D6, "NORMAL": 0xFAB219, "ZU TEUER": 0xD03B3B}


def _discord_request(method: str, url: str, payload: dict | None, bot: bool) -> dict:
    headers = {"User-Agent": DISCORD_UA, "Content-Type": "application/json"}
    if bot:
        headers["Authorization"] = "Bot " + os.environ["DISCORD_BOT_TOKEN"]
    data = json.dumps(payload).encode() if payload is not None else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method),
                                    timeout=30) as r:
            body = r.read().decode() or "{}"
            return json.loads(body)
    except urllib.error.HTTPError as e:
        raise SourceError(f"Discord HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    except (urllib.error.URLError, OSError) as e:
        raise SourceError(f"Discord nicht erreichbar: {e}")


def md_leg(leg: dict | None) -> str:
    if not leg or not leg.get("segments"):
        return "Details erst nach Detailprüfung verfügbar"
    segs, n = leg["segments"], len(leg["segments"])
    stops = "nonstop" if leg["stops"] == 0 else f"{leg['stops']} Umstieg" + ("e" if leg["stops"] > 1 else "")
    lay = ", ".join(f"{l['airport']} {fmt_dur(l['duration_min'])}" for l in leg["layovers"])
    lines = [f"**{segs[0]['from']} → {segs[-1]['to']}** · {n} {'Flug' if n == 1 else 'Flüge'} · {fmt_dur(leg['duration_min'])}",
             f"{stops}" + (f" ({lay})" if lay else "")]
    for x in segs:
        lines.append(f"`{x['flight_number']}` {x['from']} {x['dep'][11:16]} → {x['to']} {x['arr'][11:16]}"
                     + (f" ({x['arr'][8:10]}.{x['arr'][5:7]}.)" if x['arr'][:10] != x['dep'][:10] else ""))
    return "\n".join(lines)[:1024]


def discord_embed(cfg: dict, d: dict) -> dict:
    e, o = d["eval"], d["offer"]
    v = variant_by_id(cfg, d["variant"])
    days = (dt.date.fromisoformat(d["ret"]) - dt.date.fromisoformat(d["out"])).days
    icon = {"EXTREM GUT": "🔥", "GUT": "✅"}.get(e["tier"], "📉")
    desc = [f"📅 **{d['out']} → {d['ret']}** ({days} Tage)", f"✈️ {', '.join(o['leg']['airlines'])}",
            f"💡 {'; '.join(e['reasons'])}"]
    b = o.get("booking") or {}
    if b.get("book_with"):
        desc.append(f"🏷️ Buchbar bei {b['book_with']}" + (f" ({b['option_title']})" if b.get("option_title") else ""))
    fields = [
        {"name": "🛫 Hinflug", "value": md_leg(o["leg"]), "inline": False},
        {"name": "🛬 Rückflug", "value": md_leg(o.get("return_leg")), "inline": False},
        {"name": "💶 Preis", "value": f"Ticket {o['price']:.0f} € + Gepäck {e['bag_cost']:.0f} € = **{e['total']:.0f} €**"
                                       + (f"\n(Bewertung inkl. {v['penalty_eur']} € Varianten-Aufschlag)" if v.get("penalty_eur") else ""),
         "inline": True},
        {"name": "🧳 Gepäck", "value": e["bag_note"][:1024], "inline": True},
    ]
    warn = e["issues"] + (o.get("verify_notes") or [])
    if warn:
        fields.append({"name": "⚠️ Hinweise", "value": "\n".join(f"• {w}" for w in warn)[:1024], "inline": False})
    links = []
    if o.get("google_flights_url"):
        links.append(f"[Google Flights öffnen]({o['google_flights_url']})")
    if cfg["notify"].get("dashboard_url"):
        links.append(f"[Dashboard]({cfg['notify']['dashboard_url']})")
    if links:
        fields.append({"name": "🔗 Links", "value": " · ".join(links), "inline": False})
    emb = {"title": f"{icon} {e['tier']}: {e['total']:.0f} € – {v['name']}"[:256], "description": "\n".join(desc)[:4000],
           "color": TIER_COLOR.get(e["tier"], 0x77766F), "fields": fields,
           "footer": {"text": "Preise ändern sich laufend – vor Buchung prüfen. Es wird nie automatisch gebucht."},
           "timestamp": d["ts"].replace("Z", "+00:00")}
    if o.get("google_flights_url"):
        emb["url"] = o["google_flights_url"]
    return emb


def html_to_md(s: str) -> str:
    s = re.sub(r'<a href="([^"]+)">([^<]+)</a>', lambda m: f"[{m.group(2)}]({m.group(1).replace('&amp;', '&')})", s)
    s = re.sub(r"</?b>", "**", s)
    s = re.sub(r"</?i>", "*", s)
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#x27;", "'")


def send_discord(subject: str, text: str, embeds: list[dict] | None = None) -> None:
    mention = os.environ.get("DISCORD_MENTION_USER_ID", "").strip()
    embeds = embeds or [{"description": html_to_md(text)[:4000], "color": 0x2A78D6}]
    # Discord: max. 10 Embeds und 6000 Zeichen pro Nachricht -> ggf. auf mehrere Nachrichten verteilen
    chunks, cur, size = [], [], 0
    for e in embeds:
        n = len(json.dumps(e, ensure_ascii=False))
        if cur and (size + n > 5500 or len(cur) == 10):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += n
    chunks.append(cur)
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    channel = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not hook and not channel:  # Direktnachricht an DISCORD_USER_ID
        dm = _discord_request("POST", f"{DISCORD_API}/users/@me/channels",
                              {"recipient_id": os.environ["DISCORD_USER_ID"]}, bot=True)
        channel = dm["id"]
    for i, emb in enumerate(chunks):
        payload = {"content": ((f"<@{mention}> " if mention else "") + f"**{subject}**") if i == 0 else "",
                   "allowed_mentions": {"users": [mention] if mention and i == 0 else []}, "embeds": emb}
        if hook:
            payload["username"] = "goth baddie (claude bot)"
            _discord_request("POST", hook + ("&" if "?" in hook else "?") + "wait=true", payload, bot=False)
        else:
            _discord_request("POST", f"{DISCORD_API}/channels/{channel}/messages", payload, bot=True)


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:4000],
                                   "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SourceError(f"Telegram HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    except (urllib.error.URLError, OSError) as e:
        raise SourceError(f"Telegram nicht erreichbar: {e}")
    if not res.get("ok"):
        raise SourceError(f"Telegram: {res.get('description')}")


def send_email(subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("MAIL_FROM") or os.environ["SMTP_USER"], os.environ["MAIL_TO"]
    msg.set_content(html_to_text(text))
    msg.add_alternative("<div style='font-family:sans-serif;white-space:pre-wrap'>" + text + "</div>", subtype="html")
    port = int(os.environ.get("SMTP_PORT") or 465)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, context=ssl.create_default_context(), timeout=30) as s:
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise SourceError(f"E-Mail-Versand fehlgeschlagen: {e}")


def notify(subject: str, text: str, deals: list[dict] | None = None, cfg: dict | None = None) -> tuple[list[str], list[str]]:
    sent, errors = [], []
    for ch in channels():
        try:
            if ch == "discord":
                send_discord(subject, text, [discord_embed(cfg, d) for d in deals] if deals and cfg else None)
            elif ch == "telegram":
                send_telegram(text)
            else:
                send_email(subject, text)
            sent.append(ch)
        except (SourceError, KeyError) as e:
            errors.append(f"{ch}: {e}")
    return sent, errors


# --------------------------------------------------------------------------- main run
def run(cfg: dict) -> dict:
    now = now_utc()
    state = read_json("state.json", {})
    obs_all = read_observations()
    runs = read_json("runs.json", [])
    deals = read_json("deals.json", [])
    key = os.environ.get("SERPAPI_KEY", "").strip()
    rec = {"ts": iso(now), "status": "ok", "searches": 0, "verifications": 0, "errors": [], "notes": [],
           "results": [], "notified": [], "channels": channels()}

    if not key:
        rec["status"] = "error"
        rec["errors"].append("SERPAPI_KEY ist nicht gesetzt – keine Preisabfrage möglich.")
    else:
        left = serpapi_account(key)
        if left is None:
            rec["notes"].append("Kontostand bei SerpApi nicht abrufbar – nutze eigene Zählung")
        n, max_ver, note = searches_for_run(cfg, state, now, left)
        rec["notes"].append(note)
        rec["budget_left"] = left
        recheck = best_recent_combo(obs_all, now)
        combos = next_combos(cfg, state, n, now.date(), recheck)
        if not combos and n > 0:
            rec["notes"].append("Keine zukünftigen Reisedaten mehr im Suchfenster – config.toml anpassen.")
        ver_budget = [max_ver * 2]
        new_obs, candidates = [], []
        for c in combos:
            res = {"variant": c["variant"], "out": c["out"], "ret": c["ret"], "recheck": bool(c.get("recheck"))}
            o = {"ts": iso(now), **res, "status": "ok", "best_total": None}
            params = base_params(cfg, c, key)
            try:
                rec["searches"] += 1
                data = serpapi_search(params)
                gf_url = (data.get("search_metadata") or {}).get("google_flights_url")
                if data.get("_no_results"):
                    o["status"] = res["status"] = "no_results"
                    res["message"] = "Keine Flüge gefunden"
                else:
                    its = parse_itineraries(data)
                    pi = data.get("price_insights") or {}
                    o["price_insights"] = {k: pi.get(k) for k in ("lowest_price", "price_level", "typical_price_range")}
                    if not its:
                        o["status"] = res["status"] = "no_results"
                        res["message"] = "Antwort ohne Preise"
                    else:
                        base = baseline(cfg, obs_all, c["variant"], now)
                        evals = []
                        for it in its[:8]:
                            it["google_flights_url"] = gf_url
                            evals.append((evaluate(cfg, it, c["variant"], base), it))
                        valid = [x for x in evals if "Self-Transfer" not in " ".join(x[0]["issues"])] or evals
                        valid.sort(key=lambda x: x[0]["effective"])
                        ev, best = valid[0]
                        o.update(best_total=ev["total"], best_price=best["price"], tier=ev["tier"],
                                 leg=best["leg"], bag_note=ev["bag_note"], bag_cost=ev["bag_cost"],
                                 airlines=best["leg"]["airlines"], stops=best["leg"]["stops"],
                                 duration_min=best["leg"]["duration_min"], google_flights_url=gf_url,
                                 top=[{"price": x[1]["price"], "total": x[0]["total"],
                                       "airlines": x[1]["leg"]["airlines"], "stops": x[1]["leg"]["stops"],
                                       "duration_min": x[1]["leg"]["duration_min"]} for x in valid[:3]])
                        res.update(status="ok", total=ev["total"], tier=ev["tier"], airlines=best["leg"]["airlines"])
                        if ev["reasons"]:
                            candidates.append((ev["effective"], c, params, best, base))
            except SourceError as e:
                o["status"] = res["status"] = "error"
                res["message"] = o["error"] = str(e)
                rec["errors"].append(f"{c['variant']} {c['out']}→{c['ret']}: {e}")
            new_obs.append(o)
            rec["results"].append(res)
        append_observations(new_obs)
        process_candidates(cfg, state, rec, deals, candidates, ver_budget, now)
        month = now.strftime("%Y-%m")
        state.setdefault("usage", {})[month] = state.get("usage", {}).get(month, 0) + rec["searches"] + rec["verifications"]
        if combos and all(r["status"] == "error" for r in rec["results"]):
            rec["status"] = "error"
        elif rec["errors"]:
            rec["status"] = "partial"
        if n == 0:
            rec["status"] = "skipped"

    # Fehlerserie -> Systemwarnung
    if rec["status"] == "error":
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        if state["fail_streak"] == int(cfg["notify"]["failure_alert_after_runs"]):
            msg = (f"⚠️ <b>Flug-Monitor: {state['fail_streak']} Läufe in Folge fehlgeschlagen</b>\n"
                   + escape("\n".join(rec["errors"][:5])))
            sent, errs = notify("Flug-Monitor: Fehler", msg)
            rec["notes"].append(f"Systemwarnung gesendet über: {', '.join(sent) or '—'}")
            rec["errors"] += errs
    else:
        state["fail_streak"] = 0

    if cfg["notify"].get("weekly_status") and rec["status"] != "error":
        last = state.get("last_weekly")
        if not last or now - parse_iso(last) >= dt.timedelta(days=7):
            weekly_status(cfg, state, rec, now)

    if rec["status"] != "skipped" and not channels():
        rec["notes"].append("Keine Benachrichtigungskanäle konfiguriert (Discord/Telegram/E-Mail).")
    runs.append(rec)
    write_json("runs.json", runs[-400:])
    write_json("deals.json", deals[-100:])
    state["last_run"] = iso(now)
    write_json("state.json", state)
    import dashboard
    dashboard.build(cfg, DATA, DOCS)
    return rec


def best_recent_combo(obs: list[dict], now: dt.datetime) -> dict | None:
    """Günstigste Kombination der letzten 7 Tage – erneut prüfen, wenn > 20 h alt."""
    recent = [o for o in obs if o.get("best_total") and parse_iso(o["ts"]) >= now - dt.timedelta(days=7)]
    if not recent:
        return None
    best = min(recent, key=lambda o: o["best_total"])
    last_check = max(parse_iso(o["ts"]) for o in obs
                     if o["variant"] == best["variant"] and o["out"] == best["out"] and o["ret"] == best["ret"])
    if now - last_check < dt.timedelta(hours=20) or best["out"] <= now.date().isoformat():
        return None
    return {"variant": best["variant"], "out": best["out"], "ret": best["ret"]}


MAX_DEALS_PER_MESSAGE = 3


def process_candidates(cfg, state, rec, deals, candidates, ver_budget, now):
    """Kandidaten des Laufs: beste zuerst verifizieren, neu bewerten, entdoppeln, gebündelt melden."""
    candidates.sort(key=lambda x: x[0])
    to_send, verified_variants, slots = [], set(), 0
    for _, c, params, offer, base in candidates:
        offer = json.loads(json.dumps(offer))  # Kopie
        active = slots < MAX_DEALS_PER_MESSAGE and c["variant"] not in verified_variants
        if active:
            slots += 1
            verified_variants.add(c["variant"])
            before = ver_budget[0]
            try:
                verify(cfg, params, offer, ver_budget)
            except SourceError as e:
                offer.setdefault("verify_notes", []).append(f"Verifikation fehlgeschlagen: {e}")
                rec["errors"].append(f"Verifikation {c['variant']} {c['out']}: {e}")
            finally:
                rec["verifications"] += before - ver_budget[0]
        else:
            offer["verify_notes"] = ["nicht verifiziert (günstigerer Kandidat derselben Variante in diesem Lauf)"
                                     if c["variant"] in verified_variants else "zurückgestellt (Meldungslimit pro Lauf)"]
        ev = evaluate(cfg, offer, c["variant"], base)
        for tok in ("departure_token", "booking_token"):
            offer.pop(tok, None)
        d = {"ts": iso(now), "variant": c["variant"], "out": c["out"], "ret": c["ret"], "offer": offer, "eval": ev,
             "notified": False}
        d["key"] = deal_key(d)
        if ev["notify"] and active:
            same_variant = [x for x in to_send if x["variant"] == d["variant"]]
            if should_notify(cfg, state, d) and (not same_variant or ev["total"] <= min(
                    x["eval"]["total"] for x in same_variant) - float(cfg["thresholds"]["renotify_drop_eur"])):
                to_send.append(d)
            else:
                d["duplicate"] = True
        deals.append(d)
    if not to_send:
        return
    if not channels():
        for d in to_send:
            d["notify_skipped"] = "kein Kanal konfiguriert"
        return
    to_send.sort(key=lambda d: d["eval"]["effective"])
    text = "\n\n━━━━━━━━━━\n\n".join(format_deal(cfg, d) for d in to_send)
    top = to_send[0]["eval"]
    subj = f"Flug-Deal {top['tier']}: ab {top['total']:.0f} € ({len(to_send)} Angebot{'e' if len(to_send) > 1 else ''})"
    sent, errs = notify(subj, text, to_send, cfg)
    rec["errors"] += errs
    if sent:
        for d in to_send:
            d["notified"], d["channels"] = True, sent
            state.setdefault("notified", {})[d["key"]] = {"total": d["eval"]["total"], "tier": d["eval"]["tier"],
                                                          "ts": iso(now), "variant": d["variant"]}
            rec["notified"].append(d["key"])


def weekly_status(cfg, state, rec, now):
    obs = [o for o in read_observations() if o.get("best_total") and parse_iso(o["ts"]) >= now - dt.timedelta(days=7)]
    lines = ["📊 <b>Flug-Monitor Wochenstatus</b>"]
    for v in cfg["variants"]:
        vs = [o for o in obs if o["variant"] == v["id"]]
        if vs:
            b = min(vs, key=lambda o: o["best_total"])
            lines.append(f"{escape(v['name'])}: ab {b['best_total']:.0f} € ({b['out']}→{b['ret']})")
    sent, errs = notify("Flug-Monitor Wochenstatus", "\n".join(lines))
    if sent:
        state["last_weekly"] = iso(now)
    rec["errors"] += errs


# --------------------------------------------------------------------------- CLI
def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    cfg = load_config()
    if cmd == "run":
        try:
            rec = run(cfg)
        except Exception as e:  # unerwarteter Fehler: dokumentieren, Dashboard trotzdem bauen
            runs = read_json("runs.json", [])
            runs.append({"ts": iso(now_utc()), "status": "error", "searches": 0, "verifications": 0,
                         "errors": [f"Interner Fehler: {type(e).__name__}: {e}"], "notes": [], "results": [],
                         "notified": [], "channels": channels()})
            write_json("runs.json", runs[-400:])
            import dashboard
            dashboard.build(cfg, DATA, DOCS)
            raise
        print(json.dumps({k: rec[k] for k in ("ts", "status", "searches", "verifications", "errors", "notes",
                                              "notified")}, ensure_ascii=False, indent=1))
        return 0
    if cmd == "dashboard":
        import dashboard
        dashboard.build(cfg, DATA, DOCS)
        print(f"Dashboard geschrieben: {DOCS / 'index.html'}")
        return 0
    if cmd == "test-notify":
        if not channels():
            print("Kein Kanal konfiguriert (DISCORD_WEBHOOK_URL bzw. DISCORD_BOT_TOKEN+DISCORD_CHANNEL_ID, Telegram oder SMTP).")
            return 1
        sent, errs = notify("Flug-Monitor Test", "✅ <b>Testnachricht</b>\nDer Indonesia Flight Deal Monitor kann dich erreichen. Echte Deals kommen nur, wenn ein Angebot deine Kriterien erfüllt.")
        print("gesendet:", sent, "Fehler:", errs)
        return 0 if sent and not errs else 1
    if cmd == "telegram-chat-id":
        tok = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not tok:
            print("TELEGRAM_BOT_TOKEN fehlt.")
            return 1
        res = http_get_json(f"https://api.telegram.org/bot{tok}/getUpdates", {})
        ids = {str(u["message"]["chat"]["id"]) for u in res.get("result", []) if "message" in u}
        print("Chat-IDs:", ", ".join(ids) or "keine – schreibe dem Bot zuerst eine Nachricht")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
