"""Airbnb-Modul mit nachgebildeten Apify-Antworten (keine Netzwerkaufrufe, keine echten Preise)."""
import copy
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import airbnb  # noqa: E402
import monitor  # noqa: E402

CFG_FULL = monitor.load_config(ROOT / "config.toml")
TODAY = dt.date(2026, 11, 25)
WL = "https://www.airbnb.com/wishlists/v/1276806850?s=67&unique_share_id=abc"


def cfg_with(**place_updates):
    c = copy.deepcopy(CFG_FULL)
    for p in c["airbnb"]["places"]:
        if p["id"] in place_updates:
            p.update(place_updates[p["id"]])
    return c


def calendar(lid, busy=()):
    days, d = [], dt.date(2026, 12, 1)
    while d <= dt.date(2027, 9, 30):
        free = d.isoformat() not in busy
        days.append({"date": d.isoformat(), "available": free, "bookable": free, "available_for_checkin": free,
                     "available_for_checkout": True, "min_nights": 1, "max_nights": 365, "price": None})
        d += dt.timedelta(days=1)
    return {"listing_id": lid, "listing_url": f"https://www.airbnb.com/rooms/{lid}", "calendar": days}


class FakeApify:
    def __init__(self, prices):
        self.prices, self.calls = prices, []

    def __call__(self, actor, inp, token, timeout_s=290):
        self.calls.append((actor, inp))
        if "wishlist" in actor:
            return [{"name": "Sea view studio", "url": "https://www.airbnb.com/rooms/11111"},
                    {"title": "Jungle hut", "listingUrl": "https://www.airbnb.de/rooms/22222?source=x"},
                    {"name": "Loft", "id": "33333", "url": "https://www.airbnb.com/rooms/33333"}]
        if "availability" in actor:
            busy = {(dt.date(2027, 4, 1) + dt.timedelta(days=i)).isoformat() for i in range(40)}
            return [calendar("11111"), calendar("22222", busy), calendar("33333")]
        out = []
        for u in inp["startUrls"]:
            lid = u["url"].rsplit("/", 1)[1]
            p = self.prices.get(lid)
            out.append({"id": lid, "url": u["url"], "name": f"Name {lid}",
                        "price": {"label": f"€{p:,.0f} total" if p else "", "price": f"€{p:,.0f}" if p else None,
                                  "breakDown": {"basePrice": {"description": "30 nights x €9", "price": "€270"}}},
                        "rating": {"guestSatisfaction": 4.9, "reviewsCount": 87}})
        return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = monitor.DATA
        monitor.DATA = Path(self.tmp.name) / "data"
        monitor.DATA.mkdir()
        os.environ["APIFY_TOKEN"] = "test"

    def tearDown(self):
        monitor.DATA = self.old
        os.environ.pop("APIFY_TOKEN", None)
        self.tmp.cleanup()


class TestParsing(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(airbnb.listing_ids_from({"a": "https://airbnb.com/rooms/plus/98765?x", "listingId": 55555}),
                         ["98765", "55555"])

    def test_money(self):
        for s, v in (("€1,234 total", 1234), ("1.234 €", 1234), ("€ 412.50", 412.5), ("412,50 €", 412.5), (380, 380)):
            self.assertEqual(airbnb.parse_money(s), v, s)

    def test_total_prefers_total_field(self):
        item = {"price": {"price": "€15", "qualifier": "night", "breakDown": {"total": {"price": "€455"}}}}
        self.assertEqual(airbnb.extract_total(item, 30)[0], 455)

    def test_total_from_label(self):
        self.assertEqual(airbnb.extract_total({"price": {"label": "€380 total"}}, 30)[0], 380)

    def test_no_guessing(self):
        self.assertEqual(airbnb.extract_total({"name": "x", "price": {"price": "€12"}}, 30), (None, ""))

    def test_windows(self):
        s = airbnb.settings(CFG_FULL)
        cals = airbnb.parse_calendars([calendar("1"), calendar("2", {"2027-05-10"})])
        p = {"check_in_from": "2027-05-01", "check_out_to": "2027-07-31"}
        wins = airbnb.choose_windows(p, cals, ["1", "2"], s, TODAY)
        self.assertEqual(len(wins), 2)
        for w in wins:
            self.assertEqual(w["nights"], 30)
            self.assertLessEqual(w["check_out"], "2027-07-31")
            self.assertGreaterEqual(w["check_in"], "2027-05-01")
        self.assertEqual(wins[0]["free"], ["1", "2"])  # Fenster, in dem beide frei sind, zuerst

    def test_min_nights_respected(self):
        c = calendar("1")
        for d in c["calendar"]:
            d["min_nights"] = 40
        cals = airbnb.parse_calendars([c])
        self.assertFalse(airbnb.is_free(cals["1"], dt.date(2027, 5, 1), 30))


class TestRefresh(Base):
    def test_no_lists_placeholder(self):
        cfg = copy.deepcopy(CFG_FULL)
        fake = FakeApify({})
        with mock.patch.object(airbnb, "apify_run", fake):
            log = airbnb.refresh(cfg, today=TODAY)
        self.assertEqual(fake.calls, [])
        self.assertIn("Platzhalter", log["notes"][0])
        airbnb.attach(cfg)
        lines, pkg = airbnb.package_lines(cfg, "VN", "2027-05-01", "2027-05-31", 700)
        self.assertEqual(lines, ["🏠 **Da Nang / Son Tra**: Merkliste fehlt noch", "🏠 **Nha Trang**: Merkliste fehlt noch"])
        self.assertIsNone(pkg)

    def test_full_flow_and_package(self):
        cfg = cfg_with(danang={"wishlist_url": WL})
        fake = FakeApify({"11111": 280, "22222": 300, "33333": 520})
        with mock.patch.object(airbnb, "apify_run", fake):
            log = airbnb.refresh(cfg, today=TODAY)
        self.assertEqual(log["errors"], [])
        self.assertEqual(fake.calls[0][1]["wishlistUrls"], [{"url": WL}])
        st = json.loads((monitor.DATA / "airbnb" / "stays.json").read_text())
        wins = st["places"]["danang"]["windows"]
        self.assertEqual(len(wins), 2)
        best = airbnb.best_stay(wins[0])
        self.assertEqual(best["total"], 280)
        self.assertIn("check_in=", best["url"])
        over = [x for w in wins for x in w["stays"] if x["id"] == "33333"]
        self.assertFalse(over[0]["in_budget"])
        self.assertTrue((monitor.DATA / "airbnb" / "raw_price.json").exists())
        # zweiter Lauf innerhalb einer Woche: keine Kosten
        with mock.patch.object(airbnb, "apify_run", fake):
            n = len(fake.calls)
            airbnb.refresh(cfg, today=TODAY)
            self.assertEqual(len(fake.calls), n)
        # Kopplung mit Flug: synthetische Route + Paketzeile
        airbnb.attach(cfg)
        cfg.pop("_trips", None)
        w = wins[0]
        v = monitor.variant_by_id(cfg, "AB-danang")
        self.assertEqual(v["outbound_to"], "DAD")
        out = (dt.date.fromisoformat(w["check_in"]) - dt.timedelta(days=1)).isoformat()
        self.assertIn([out, w["check_out"]], v["windows"])
        plans = monitor.all_combos(cfg, TODAY)
        self.assertTrue(plans["AB-danang"])
        lines, pkg = airbnb.package_lines(cfg, "VN", out, w["check_out"], 698, "danang")
        self.assertEqual(round(pkg["total"]), 698 + 280 + 15)
        self.assertTrue(any("Gesamt 993 €" in ln for ln in lines))
        self.assertTrue(any("rooms/33333" in ln and "über Budget" in ln for ln in lines))
        # Deal ohne passende Daten -> Hinweis auf bestes Fenster
        lines, pkg = airbnb.package_lines(cfg, "VN", "2027-07-01", "2027-07-20", 698)
        self.assertIsNone(pkg)
        self.assertIn("andere Daten", lines[0])

    def test_embed_contains_airbnb_field(self):
        cfg = copy.deepcopy(CFG_FULL)
        airbnb.attach(cfg)
        d = {"ts": "2026-11-25T05:00:00Z", "variant": "VN2", "out": "2027-05-01", "ret": "2027-05-31",
             "offer": {"price": 650, "leg": {"airlines": ["X"], "segments": [], "stops": 1, "layovers": [],
                                             "duration_min": 900}},
             "eval": {"tier": "GUT", "total": 700, "reasons": ["r"], "bag_cost": 0, "bag_note": "inkl.",
                      "issues": [], "score": 700, "effective": 700}}
        emb = monitor.discord_embed(cfg, d)
        f = [x for x in emb["fields"] if x["name"].startswith("🏠")]
        self.assertEqual(len(f), 1)
        self.assertIn("Merkliste fehlt noch", f[0]["value"])
        self.assertIn("Merkliste fehlt", monitor.format_deal(cfg, d))


if __name__ == "__main__":
    unittest.main()
