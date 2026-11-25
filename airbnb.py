"""Airbnb-Merklisten → freie Zeitfenster → Preise → Pakete mit Flug.

Ablauf (``python airbnb.py refresh``, täglich per GitHub Actions, arbeitet aber nur wenn nötig):
 1. Merklisten (view-only-Links) lesen → Unterkunfts-IDs (neue Herzen werden automatisch übernommen).
 2. Verfügbarkeitskalender aller Unterkünfte → freie Zeitfenster mit 20–35 Nächten in der regenfreien Zeit des Orts.
 3. Echte Preise für die besten Zeitfenster (Airbnb-Abfrage mit check-in/check-out, EUR, 1 Gast).
 4. Ergebnis in data/airbnb/stays.json – der Flug-Monitor sucht dann gezielt Flüge zu diesen Fenstern
    und zeigt in Discord „Flug + Airbnb + Transfer = Gesamt“.

Datenquelle: Apify-Actors (Token als Secret APIFY_TOKEN). Preise werden nie geschätzt: wenn ein Preis
nicht lesbar ist, steht „Preis unbekannt“ da und die Unterkunft zählt nicht für Pakete.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import monitor
from monitor import SourceError, iso, now_utc, parse_iso

APIFY_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


# --------------------------------------------------------------------------- io
def ab_dir() -> Path:
    return monitor.DATA / "airbnb"


def _read(name: str, default):
    try:
        return json.loads((ab_dir() / name).read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(name: str, obj) -> None:
    ab_dir().mkdir(parents=True, exist_ok=True)
    (ab_dir() / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")


def settings(cfg: dict) -> dict:
    a = cfg.get("airbnb", {})
    return {"enabled": a.get("enabled", True), "max_total_eur": float(a.get("max_total_eur", 450)),
            "min_nights": int(a.get("min_nights", 20)), "max_nights": int(a.get("max_nights", 35)),
            "preferred_nights": [int(x) for x in a.get("preferred_nights", [30, 25, 21])],
            "refresh_days": int(a.get("refresh_days", 7)), "windows_per_place": int(a.get("windows_per_place", 2)),
            "max_listings_per_place": int(a.get("max_listings_per_place", 10)),
            "max_price_checks": int(a.get("max_price_checks_per_refresh", 60)),
            "actors": {"wishlist": "automation-lab/airbnb-shared-wishlist-scraper",
                       "calendar": "nobin2k/airbnb-availability",
                       "price": "tri_angle/airbnb-rooms-urls-scraper"} | a.get("actors", {}),
            "places": a.get("places", [])}


def attach(cfg: dict) -> dict:
    """Lädt die letzten Airbnb-Ergebnisse in die Konfiguration (für Flugsuche, Meldungen, Dashboard)."""
    cfg["_airbnb"] = {"stays": _read("stays.json", {}), "listings": _read("listings.json", {})}
    return cfg


# --------------------------------------------------------------------------- helpers
ROOM_RE = re.compile(r"/rooms/(?:plus/)?(\d{4,25})")


def listing_ids_from(obj) -> list[str]:
    """Unterkunfts-IDs aus beliebigem JSON (Links /rooms/<id> oder id-Felder)."""
    found: list[str] = []

    def add(x):
        x = str(x)
        if x.isdigit() and len(x) >= 4 and x not in found:
            found.append(x)

    def walk(o, key=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, k.lower())
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str):
            for m in ROOM_RE.finditer(o):
                add(m.group(1))
            if key in ("listingid", "listing_id", "roomid", "room_id", "staysid", "stayid") and o.isdigit():
                add(o)
        elif isinstance(o, int) and key in ("listingid", "listing_id", "roomid", "room_id", "stayid"):
            add(o)

    walk(obj)
    return found


def _first(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def room_url(lid: str, ci: str | None = None, co: str | None = None) -> str:
    q = f"?check_in={ci}&check_out={co}&adults=1" if ci and co else ""
    return f"https://www.airbnb.com/rooms/{lid}{q}"


def parse_money(v) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str):
        return None
    m = re.search(r"\d[\d.,\s  ]*", v)
    if not m:
        return None
    s = re.sub(r"[\s  ]", "", m.group(0)).rstrip(".,")
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", "") if re.search(r",\d{3}$", s) else s.replace(",", ".")
    elif re.search(r"\.\d{3}$", s) and s.count(".") >= 1 and not re.search(r"\.\d{1,2}$", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def extract_total(item: dict, nights: int) -> tuple[float | None, str]:
    """Gesamtpreis eines Aufenthalts aus der Actor-Ausgabe (Feldnamen nicht dokumentiert → defensiv).

    Rückgabe (Preis, Quelle). Reihenfolge: Feld mit „total“ (ohne „before“) → „total before taxes“ →
    Preis-Text mit „total“ → Nachtpreis × Nächte (markiert als ≈). Kein Treffer → (None, "")."""
    cands: list[tuple[int, float, str]] = []

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, path + [str(k).lower()])
            return
        if isinstance(o, list):
            for v in o[:30]:
                walk(v, path)
            return
        p = ".".join(path)
        if "price" not in p and "total" not in p and "rate" not in p:
            return
        if any(x in p for x in ("original", "strike", "discount", "fee", "tax", "cleaning", "service", "photo",
                                "review", "currency", "deposit")) and "total" not in p:
            return
        val = parse_money(o)
        if val is None or val <= 0:
            return
        txt = o if isinstance(o, str) else ""
        last = path[-1] if path else ""
        if "total" in p and "before" not in p:
            cands.append((0, val, p))
        elif "total" in p:
            cands.append((1, val, p))
        elif "total" in txt.lower():
            cands.append((2, val, p))
        elif "night" in p or "night" in txt.lower() or last in ("rate", "nightly", "pricepernight"):
            cands.append((4, val * nights, p + " ×Nächte"))
        elif last in ("price", "amount", "value", "label") and "breakdown" not in p:
            cands.append((3, val, p))

    walk(item, [])
    ok = [c for c in cands if nights * 2 <= c[1] <= 60000]
    if not ok:
        return None, ""
    ok.sort(key=lambda c: (c[0], -c[1]))
    rank, val, path = ok[0]
    if rank == 3 and val < nights * 15:  # sieht nach Nachtpreis aus, nicht nach Gesamtpreis -> nicht raten
        return None, ""
    return round(val, 2), ("≈ " if rank == 4 else "") + path


def unavailable(item: dict) -> bool:
    s = json.dumps(item, ensure_ascii=False).lower()[:20000]
    return any(x in s for x in ('"available": false', '"isavailable": false', "not available for these dates",
                                "those dates are not available"))


# --------------------------------------------------------------------------- apify
def apify_run(actor: str, inp: dict, token: str, timeout_s: int = 290) -> list[dict]:
    url = APIFY_URL.format(actor=actor.replace("/", "~")) + "?" + urllib.parse.urlencode(
        {"token": token, "timeout": timeout_s, "format": "json", "clean": "true"})
    req = urllib.request.Request(url, data=json.dumps(inp).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s + 60) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "[]")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise SourceError(f"Apify {actor}: HTTP {e.code} {body}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise SourceError(f"Apify {actor} nicht erreichbar: {e}")
    if isinstance(data, dict):
        data = data.get("items") or data.get("data") or [data]
    return [x for x in data if isinstance(x, dict)]


def save_raw(kind: str, items: list[dict]) -> None:
    """Beispielausgabe speichern (gekürzt), um das Auslesen an die echte Ausgabe anzupassen."""
    def trim(o, depth=0):
        if depth > 5:
            return "…"
        if isinstance(o, dict):
            return {k: trim(v, depth + 1) for k, v in list(o.items())[:60]}
        if isinstance(o, list):
            return [trim(v, depth + 1) for v in o[:5]]
        return o[:300] if isinstance(o, str) else o
    _write(f"raw_{kind}.json", [trim(x) for x in items[:3]])


# --------------------------------------------------------------------------- calendar & windows
def parse_calendars(items: list[dict]) -> dict[str, dict[str, dict]]:
    """{listing_id: {YYYY-MM-DD: {avail, checkin, checkout, min, max}}}"""
    out: dict[str, dict[str, dict]] = {}
    for it in items:
        lid = _first(it, "listing_id", "listingId", "id", "room_id", "roomId")
        lid = str(lid) if lid is not None else (listing_ids_from(it) or [None])[0]
        if not lid:
            continue
        days = None
        for k in ("calendar", "days", "dailyCalendar", "daily_calendar", "availability"):
            if isinstance(it.get(k), list):
                days = it[k]
                break
        if days is None:
            months = it.get("calendar_months") or it.get("calendarMonths") or []
            days = [d for m in months if isinstance(m, dict) for d in (m.get("days") or [])]
        cal = out.setdefault(lid, {})
        for d in days or []:
            if not isinstance(d, dict):
                continue
            date = str(_first(d, "date", "calendarDate", "day") or "")[:10]
            if not re.match(r"\d{4}-\d{2}-\d{2}$", date):
                continue
            av = _first(d, "available", "isAvailable", "availability")
            if isinstance(av, str):
                av = av.lower() in ("true", "available", "yes", "1")
            cal[date] = {"avail": bool(av) and d.get("bookable", True) is not False,
                         "checkin": d.get("available_for_checkin", d.get("availableForCheckin")),
                         "checkout": d.get("available_for_checkout", d.get("availableForCheckout")),
                         "min": _first(d, "min_nights", "minNights"), "max": _first(d, "max_nights", "maxNights")}
    return out


def is_free(cal: dict[str, dict], ci: dt.date, nights: int) -> bool:
    first = cal.get(ci.isoformat())
    if not first or first.get("checkin") is False:
        return False
    mn, mx = first.get("min"), first.get("max")
    if isinstance(mn, (int, float)) and mn > nights:
        return False
    if isinstance(mx, (int, float)) and 0 < mx < nights:
        return False
    for i in range(nights):
        d = cal.get((ci + dt.timedelta(days=i)).isoformat())
        if not d or not d["avail"]:
            return False
    co = cal.get((ci + dt.timedelta(days=nights)).isoformat())
    return not (co and co.get("checkout") is False)


def place_range(p: dict, today: dt.date) -> tuple[dt.date, dt.date]:
    start = max(dt.date.fromisoformat(p["check_in_from"]), today + dt.timedelta(days=2))
    return start, dt.date.fromisoformat(p["check_out_to"])


def choose_windows(p: dict, cals: dict[str, dict], ids: list[str], s: dict, today: dt.date) -> list[dict]:
    """Beste Zeitfenster des Orts: möglichst viele Unterkünfte frei, bevorzugte Nächte (30 vor 25 vor 21)."""
    start, end = place_range(p, today)
    prefs = [n for n in s["preferred_nights"] if s["min_nights"] <= n <= s["max_nights"]] or [s["min_nights"]]
    cands = []
    d = start
    while d < end:
        for rank, n in enumerate(prefs):
            if d + dt.timedelta(days=n) > end:
                continue
            free = [lid for lid in ids if lid in cals and is_free(cals[lid], d, n)]
            if free:
                cands.append((-len(free), rank, d, n, free))
        d += dt.timedelta(days=3)
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    picked: list[dict] = []
    for _, _, d, n, free in cands:
        if any(abs((d - dt.date.fromisoformat(w["check_in"])).days) < 14 for w in picked):
            continue
        picked.append({"check_in": d.isoformat(), "check_out": (d + dt.timedelta(days=n)).isoformat(),
                       "nights": n, "free": free[:s["max_listings_per_place"]]})
        if len(picked) >= s["windows_per_place"]:
            break
    return sorted(picked, key=lambda w: w["check_in"])


# --------------------------------------------------------------------------- refresh
def place_listing_ids(p: dict) -> list[str]:
    return listing_ids_from({"listings": p.get("listings", [])})


def refresh(cfg: dict, force: bool = False, today: dt.date | None = None) -> dict:
    s = settings(cfg)
    now = now_utc()
    today = today or now.date()
    token = os.environ.get("APIFY_TOKEN", "").strip()
    listings = _read("listings.json", {})
    stays = _read("stays.json", {})
    stays.setdefault("places", {})
    log = {"ts": iso(now), "notes": [], "errors": [], "apify_calls": 0}
    places = [p for p in s["places"] if p.get("id")]
    active = [p for p in places if p.get("wishlist_url") or p.get("listings")]
    if not s["enabled"]:
        log["notes"].append("Airbnb-Modul deaktiviert ([airbnb] enabled = false)")
    elif not active:
        log["notes"].append("Noch keine Merkliste eingetragen – Discord zeigt Platzhalter.")
    elif not token:
        log["errors"].append("APIFY_TOKEN fehlt (GitHub → Settings → Secrets → Actions).")
    if not s["enabled"] or not active or not token:
        _finish(stays, log, places)
        return log

    def stale(pid: str) -> bool:
        ts = (stays["places"].get(pid) or {}).get("ts")
        return force or not ts or now - parse_iso(ts) >= dt.timedelta(days=s["refresh_days"])

    todo = [p for p in active if stale(p["id"]) or (stays["places"].get(p["id"]) or {}).get("source") != _src(p)]
    if not todo:
        log["notes"].append("Alles aktuell – nichts abgefragt.")
        _finish(stays, log, places)
        return log

    # 1) Merklisten
    wl = [p for p in todo if p.get("wishlist_url")]
    if wl:
        try:
            log["apify_calls"] += 1
            items = apify_run(s["actors"]["wishlist"], {
                "wishlistUrls": [{"url": p["wishlist_url"]} for p in wl], "maxListingsPerWishlist": 0,
                "currency": "EUR", "locale": "en-US", "includePhotos": False}, token)
            save_raw("wishlist", items)
            for p in wl:
                wid = (re.search(r"/wishlists/(?:v/)?(\d+)", p["wishlist_url"]) or [None, None])[1]
                mine = [x for x in items if not wid or wid in json.dumps(x)] if len(wl) > 1 else items
                ids, names = [], {}
                for x in mine:
                    for lid in listing_ids_from(x)[:1] or [str(_first(x, "id", "listingId") or "")]:
                        if lid.isdigit() and lid not in ids:
                            ids.append(lid)
                            nm = _first(x, "name", "title", "listingName")
                            if isinstance(nm, str):
                                names[lid] = nm[:80]
                listings[p["id"]] = {"ids": ids, "names": names, "ts": iso(now), "source": "wishlist"}
                if not ids:
                    log["errors"].append(f"{p['name']}: Merkliste gelesen, aber keine Unterkünfte erkannt "
                                         f"(Link view-only? Ausgabe siehe data/airbnb/raw_wishlist.json)")
        except SourceError as e:
            log["errors"].append(f"Merklisten: {e}")
    for p in todo:
        manual = place_listing_ids(p)
        if manual:
            cur = listings.setdefault(p["id"], {"ids": [], "names": {}, "ts": iso(now), "source": "manual"})
            cur["ids"] = list(dict.fromkeys(cur["ids"] + manual))
    _write("listings.json", listings)

    # 2) Kalender
    all_ids = list(dict.fromkeys(i for p in todo for i in (listings.get(p["id"]) or {}).get("ids", [])))
    cals: dict = {}
    if all_ids:
        try:
            log["apify_calls"] += 1
            items = apify_run(s["actors"]["calendar"], {"listings": [room_url(i) for i in all_ids], "months": 12,
                                                        "currency": "EUR", "includeDailyCalendar": True}, token)
            save_raw("calendar", items)
            cals = parse_calendars(items)
            if not cals:
                log["errors"].append("Kalender: keine Tage erkannt (siehe data/airbnb/raw_calendar.json)")
        except SourceError as e:
            log["errors"].append(f"Kalender: {e}")

    # 3) Zeitfenster + Preise
    jobs = []
    for p in todo:
        ids = (listings.get(p["id"]) or {}).get("ids", [])
        wins = choose_windows(p, cals, ids, s, today) if cals else []
        stays["places"][p["id"]] = {"ts": iso(now), "source": _src(p), "listings": len(ids),
                                    "calendars": sum(i in cals for i in ids), "windows": wins}
        for w in wins:
            jobs.append((p, w))
    budget = s["max_price_checks"]
    batches = []
    for p, w in jobs:
        take = w["free"][:max(0, budget)]
        budget -= len(take)
        if take:
            batches.append((p, w, take))
        if budget <= 0:
            log["notes"].append("Preisprüfungen gedeckelt (max_price_checks_per_refresh).")
            break

    def price_batch(job):
        p, w, ids = job
        try:
            items = apify_run(s["actors"]["price"], {"startUrls": [{"url": room_url(i)} for i in ids],
                                                     "checkIn": w["check_in"], "checkOut": w["check_out"],
                                                     "currency": "EUR", "locale": "en-US", "adults": 1}, token)
            return job, items, None
        except SourceError as e:
            return job, [], str(e)

    names_all = {i: n for v in listings.values() for i, n in (v.get("names") or {}).items()}
    raw_saved = False
    with ThreadPoolExecutor(max_workers=3) as ex:
        for (p, w, ids), items, err in ex.map(price_batch, batches):
            log["apify_calls"] += 1
            if err:
                log["errors"].append(f"Preise {p['name']} {w['check_in']}: {err}")
                continue
            if items and not raw_saved:
                save_raw("price", items)
                raw_saved = True
            by_id = {}
            for it in items:
                lid = next((x for x in listing_ids_from(it) if x in ids), None) or str(_first(it, "id") or "")
                by_id.setdefault(lid, it)
            res = []
            for lid in ids:
                it = by_id.get(lid)
                total, src = extract_total(it, w["nights"]) if it else (None, "")
                if it and unavailable(it):
                    total, src = None, "nicht verfügbar"
                rating = (it or {}).get("rating") if isinstance((it or {}).get("rating"), dict) else {}
                res.append({"id": lid, "name": (_first(it or {}, "name", "title") or names_all.get(lid)
                                                or f"Unterkunft {lid}")[:80],
                            "url": room_url(lid, w["check_in"], w["check_out"]), "total": total, "price_src": src,
                            "rating": _first(rating, "guestSatisfaction", "overall", "value") if rating else
                            _first(it or {}, "stars", "ratingValue"),
                            "reviews": _first(rating, "reviewsCount", "count") if rating else None,
                            "in_budget": total is not None and total <= s["max_total_eur"]})
            res.sort(key=lambda r: (r["total"] is None, r["total"] or 0))
            w["stays"] = res
    for pid, v in stays["places"].items():
        for w in v.get("windows", []):
            w.pop("free", None)
    _finish(stays, log, places)
    return log


def _src(p: dict) -> str:
    return (p.get("wishlist_url") or "") + "|" + ",".join(place_listing_ids(p))


def _finish(stays: dict, log: dict, places: list[dict]) -> None:
    ids = {p["id"] for p in places}
    stays["places"] = {k: v for k, v in stays.get("places", {}).items() if k in ids}
    stays["last_refresh"] = log
    _write("stays.json", stays)


# --------------------------------------------------------------------------- packages (für monitor.py)
def place_by_id(cfg: dict, pid: str) -> dict | None:
    return next((p for p in settings(cfg)["places"] if p.get("id") == pid), None)


def best_stay(win: dict) -> dict | None:
    ok = [s for s in win.get("stays", []) if s.get("in_budget")]
    return min(ok, key=lambda s: s["total"]) if ok else None


def synthetic_routes(cfg: dict, today: dt.date | None = None) -> dict[str, list[dict]]:
    """Flugrouten zu den gefundenen Airbnb-Fenstern: {trip_id: [route]}. Abflug 1 Tag vor Check-in."""
    data = (cfg.get("_airbnb") or {}).get("stays", {}).get("places", {})
    today = today or dt.date.today()
    out: dict[str, list[dict]] = {}
    for p in settings(cfg)["places"]:
        wins = [w for w in (data.get(p.get("id")) or {}).get("windows", []) if best_stay(w)]
        wins = [w for w in wins if dt.date.fromisoformat(w["check_in"]) - dt.timedelta(days=1) > today]
        if not wins or not p.get("airports"):
            continue
        ap = ",".join(p["airports"])
        tid = (p.get("trips") or [None])[0]
        out.setdefault(tid, []).append({
            "id": f"AB-{p['id']}", "name": f"🏠 Flug zum Airbnb-Fenster {p['name']}", "outbound_to": ap,
            "return_from": ap, "weight": int(p.get("flight_weight", 1)), "penalty_eur": 0, "ground": [],
            "airbnb_place": p["id"],
            "windows": [[(dt.date.fromisoformat(w["check_in"]) - dt.timedelta(days=1)).isoformat(), w["check_out"]]
                        for w in wins]})
    return out


def _match_window(p_data: dict, out: str, ret: str) -> dict | None:
    o, r = dt.date.fromisoformat(out), dt.date.fromisoformat(ret)
    for w in p_data.get("windows", []):
        ci, co = dt.date.fromisoformat(w["check_in"]), dt.date.fromisoformat(w["check_out"])
        if o <= ci <= o + dt.timedelta(days=2) and r - dt.timedelta(days=2) <= co <= r:
            return w
    return None


def _fmt_date(s: str) -> str:
    return f"{s[8:10]}.{s[5:7]}."


def package_lines(cfg: dict, trip_id: str, out: str, ret: str, flight_total: float,
                  place_id: str | None = None, limit: int = 1000) -> tuple[list[str], dict | None]:
    """Markdown-Zeilen für Discord + bestes Paket {place, stay, total} (oder None)."""
    s = settings(cfg)
    if not s["enabled"]:
        return [], None
    data = (cfg.get("_airbnb") or {}).get("stays", {}).get("places", {})
    places = [p for p in s["places"] if (place_id and p.get("id") == place_id)
              or (not place_id and trip_id in (p.get("trips") or []))]
    lines, best = [], None
    for p in places[:5]:
        pd = data.get(p["id"]) or {}
        tr = p.get("transfer_eur", [0, 0])
        tmid = (tr[0] + tr[1]) / 2 if tr else 0
        if not (p.get("wishlist_url") or p.get("listings")):
            lines.append(f"🏠 **{p['name']}**: Merkliste fehlt noch")
            continue
        if not pd.get("windows"):
            msg = "Merkliste eingetragen – freie Zeitfenster & Preise kommen mit dem nächsten Airbnb-Check" \
                if not pd else f"kein freies Fenster mit {s['min_nights']}–{s['max_nights']} Nächten gefunden"
            lines.append(f"🏠 **{p['name']}**: {msg}")
            continue
        w = _match_window(pd, out, ret)
        if w and best_stay(w):
            b = best_stay(w)
            tot = flight_total + b["total"] + tmid
            if not best or tot < best["total"]:
                best = {"place": p, "stay": b, "window": w, "total": tot, "transfer": tmid}
            lines.append(f"🏠 **{p['name']}** {_fmt_date(w['check_in'])}–{_fmt_date(w['check_out'])} "
                         f"({w['nights']} N): [{b['name'][:40]}]({b['url']}) **{b['total']:.0f} €**")
            lines.append(f"🧮 **Flug {flight_total:.0f} € + Airbnb {b['total']:.0f} € + Transfer ~{tmid:.0f} € "
                         f"= Gesamt {tot:.0f} €**")
            others = [x for x in w.get("stays", []) if x["id"] != b["id"]][:4]
            for x in others:
                pr = f"{x['total']:.0f} €" + ("" if x["in_budget"] else " (über Budget)") if x["total"] is not None \
                    else (x.get("price_src") or "Preis unbekannt")
                lines.append(f"  • [{x['name'][:36]}]({x['url']}) {pr}")
        else:
            cands = [(best_stay(x), x) for x in pd["windows"] if best_stay(x)]
            if cands:
                b, x = min(cands, key=lambda c: c[0]["total"])
                lines.append(f"🏠 **{p['name']}**: andere Daten als dieser Flug – bestes Fenster "
                             f"{_fmt_date(x['check_in'])}–{_fmt_date(x['check_out'])}: [{b['name'][:36]}]({b['url']}) "
                             f"{b['total']:.0f} € (Flug dazu wird gezielt gesucht)")
            else:
                lines.append(f"🏠 **{p['name']}**: frei, aber kein Preis ≤ {s['max_total_eur']:.0f} € gefunden")
    out_lines, size = [], 0
    for ln in lines:
        if size + len(ln) + 1 > limit:
            out_lines.append("…")
            break
        out_lines.append(ln)
        size += len(ln) + 1
    return out_lines, best


def summary_text(cfg: dict) -> str:
    st = (cfg.get("_airbnb") or {}).get("stays", {})
    lines = []
    for p in settings(cfg)["places"]:
        pd = (st.get("places") or {}).get(p["id"]) or {}
        if not (p.get("wishlist_url") or p.get("listings")):
            lines.append(f"{p['name']}: Merkliste fehlt")
            continue
        wins = pd.get("windows", [])
        best = [best_stay(w) for w in wins if best_stay(w)]
        lines.append(f"{p['name']}: {pd.get('listings', 0)} Unterkünfte, {len(wins)} Fenster"
                     + (f", ab {min(b['total'] for b in best):.0f} €" if best else ""))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "refresh"
    cfg = monitor.load_config()
    if cmd in ("refresh", "refresh-force"):
        log = refresh(cfg, force=cmd == "refresh-force" or os.environ.get("AIRBNB_FORCE") == "1")
        attach(cfg)
        print(json.dumps(log, ensure_ascii=False, indent=1))
        print(summary_text(cfg))
        import dashboard
        dashboard.build(cfg, monitor.DATA, monitor.DOCS)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
