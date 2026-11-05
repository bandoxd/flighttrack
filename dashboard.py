"""Erzeugt das statische Dashboard docs/index.html aus den JSON-Daten (keine externen Abhängigkeiten)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def _load(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _obs(p: Path) -> list[dict]:
    if not p.exists():
        return []
    rows = []
    for line in p.read_text("utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def build(cfg: dict, data_dir: Path, docs_dir: Path) -> Path:
    import monitor  # Kombinationen zählen, Reisen normalisieren
    runs = _load(data_dir / "runs.json", [])
    deals = _load(data_dir / "deals.json", [])
    state = _load(data_dir / "state.json", {})
    obs = _obs(data_dir / "observations.jsonl")
    today = dt.date.today()
    combos = monitor.all_combos(cfg, today)
    checked = {(o["variant"], o["out"], o["ret"]) for o in obs}
    coverage = {vid: {"total": len(lst), "checked": sum((c["variant"], c["out"], c["ret"]) in checked for c in lst)}
                for vid, lst in combos.items()}
    trips = []
    for tr in monitor.trips(cfg):
        trips.append({
            "id": tr["id"], "name": tr["name"], "emoji": tr.get("emoji", ""), "priority": tr.get("priority", ""),
            "season": tr.get("season", ""), "share": tr.get("share", 1), "origins": tr["origins"],
            "threshold_mode": tr.get("threshold_mode"), "relative_pct": tr.get("relative_pct"),
            "allow_separate": bool(tr.get("allow_separate_tickets")),
            "windows": tr.get("windows", []), "ground": tr.get("ground", []),
            "variants": [{"id": v["id"], "name": v["name"], "outbound_to": v["outbound_to"],
                          "return_from": v["return_from"], "penalty_eur": v.get("penalty_eur", 0),
                          "ground": v.get("ground", []), "weight": int(v.get("weight", 1))} for v in tr["variants"]]})
    recent_cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=21)).isoformat()[:19]
    vtrip = {v["id"]: t["id"] for t in trips for v in t["variants"]}
    obs_keys = ("ts", "variant", "out", "ret", "status", "best_total", "tier", "airlines", "stops", "duration_min",
                "google_flights_url", "error", "best_price", "bag_note", "bag_cost", "effective", "reference",
                "pct_below_ref", "separate", "score")
    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "demo": (data_dir / "DEMO").exists(),
        "config": {"trip": cfg["trip"], "thresholds": cfg["thresholds"], "budget": cfg["budget"],
                   "airport_access": cfg.get("airport_access", {}), "trips": trips},
        "state": {k: state.get(k) for k in ("usage", "fail_streak", "last_run", "mc_single_origin")},
        "coverage": coverage,
        "runs": [{k: r.get(k) for k in ("ts", "status", "searches", "verifications", "errors", "notes", "results",
                                        "notified", "channels", "budget_left")} for r in runs[-60:]],
        "obs": [{k: o.get(k) for k in obs_keys} | {"trip": vtrip.get(o.get("variant"), "?")}
                | ({"leg": o.get("leg")} if o.get("leg") and o["ts"] >= recent_cut else {})
                for o in obs[-4000:] if o.get("variant") in vtrip],
        "deals": [d | {"trip": vtrip.get(d.get("variant"), "?")} for d in deals[-80:] if d.get("variant") in vtrip],
    }
    html = TEMPLATE.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    docs_dir.mkdir(parents=True, exist_ok=True)
    out = docs_dir / "index.html"
    out.write_text(html, "utf-8")
    (docs_dir / ".nojekyll").write_text("")
    return out


TEMPLATE = (Path(__file__).resolve().parent / "dashboard_template.html").read_text("utf-8")
