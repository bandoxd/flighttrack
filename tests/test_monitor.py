"""Tests mit nachgebildeten SerpApi-Antworten (keine echten Netzwerkaufrufe, keine echten Preise)."""
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

import monitor  # noqa: E402

import copy

CFG_FULL = monitor.load_config(ROOT / "config.toml")
# Alte Tests laufen mit der reinen Indonesien-Konfiguration -> prüft, dass sich das bisherige Verhalten nicht ändert
CFG = copy.deepcopy({k: v for k, v in CFG_FULL.items() if k not in ("trips", "airport_access")})
# Regressionstests der ursprünglichen Indonesien-Logik mit den ursprünglichen Einstellungen
CFG["trip"]["depart_step_days"] = 4
for _v, _w in zip(CFG["variants"], (3, 2, 2, 1)):
    _v["weight"] = _w


def full_cfg():
    return copy.deepcopy(CFG_FULL)


def seg(airline, fn, a, b, dep, arr, d):
    return {"airline": airline, "flight_number": fn, "duration": d,
            "departure_airport": {"id": a, "time": dep}, "arrival_airport": {"id": b, "time": arr}}


def itin(price, airline="Qatar Airways", stops=1, layover=150, ext=None, dtok="DT1", btok=None):
    if stops == 0:
        fl = [seg(airline, "XX 1", "FRA", "SUB", "2027-03-09 21:00", "2027-03-10 18:00", 900)]
        lays = []
    else:
        fl = [seg(airline, "QR 70", "FRA", "DOH", "2027-03-09 16:30", "2027-03-09 23:55", 385),
              seg(airline, "QR 958", "DOH", "SUB", "2027-03-10 02:20", "2027-03-10 16:10", 590)]
        lays = [{"id": "DOH", "duration": layover}]
        if stops == 2:
            fl.append(seg(airline, "XX 9", "SUB", "SUB", "2027-03-10 17:00", "2027-03-10 18:00", 60))
            lays.append({"id": "CGK", "duration": 90})
    it = {"price": price, "flights": fl, "layovers": lays, "total_duration": sum(f["duration"] for f in fl) + layover,
          "extensions": ext or []}
    if dtok:
        it["departure_token"] = dtok
    if btok:
        it["booking_token"] = btok
    return it


def search_resp(items):
    return {"search_metadata": {"status": "Success", "google_flights_url": "https://www.google.com/travel/flights?test=1"},
            "best_flights": items[:1], "other_flights": items[1:],
            "price_insights": {"lowest_price": items[0]["price"] if items else None}}


def return_resp(price):
    r = itin(price, dtok=None, btok="BT1")
    r["flights"] = [seg("Qatar Airways", "QR 963", "DPS", "DOH", "2027-04-08 19:25", "2027-04-09 00:15", 590),
                    seg("Qatar Airways", "QR 69", "DOH", "FRA", "2027-04-09 02:05", "2027-04-09 07:20", 435)]
    r["layovers"] = [{"id": "DOH", "duration": 110}]
    return search_resp([r])


def booking_resp(price, bags=("1 free carry-on", "1st checked bag free")):
    return {"search_metadata": {"status": "Success"}, "booking_options": [
        {"together": {"book_with": "Qatar Airways", "price": price, "option_title": "Economy Classic",
                      "baggage_prices": list(bags)}}]}


class FakeSerp:
    """Simuliert SerpApi anhand der Parameter."""

    def __init__(self, first_price=690, ret_price=None, book_price=None, fail=False, bags=None, typical=None,
                 reject_multi_origin=False):
        self.first, self.ret = first_price, ret_price or first_price
        self.book = book_price or self.ret
        self.fail, self.bags, self.typical = fail, bags, typical
        self.reject_multi_origin = reject_multi_origin
        self.calls = []

    def __call__(self, url, params, retries=1):
        self.calls.append(dict(params))
        if "account" in url:
            return {"total_searches_left": 200}
        if self.fail:
            raise monitor.SourceError("HTTP 503: Service unavailable")
        if "booking_token" in params:
            return booking_resp(self.book, self.bags) if self.bags is not None else booking_resp(self.book)
        if self.reject_multi_origin and params.get("type") == 3 and "FRA,DUS" in params.get("multi_city_json", ""):
            raise monitor.SourceError("HTTP 400: Unsupported departure_id in multi_city_json")
        if "departure_token" in params:
            return return_resp(self.ret)
        r = search_resp([itin(self.first), itin(self.first + 180, airline="Lufthansa")])
        if self.typical:
            r["price_insights"]["typical_price_range"] = list(self.typical)
        return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        monitor.DATA = Path(self.tmp.name) / "data"
        monitor.DOCS = Path(self.tmp.name) / "docs"
        self.env = mock.patch.dict(os.environ, {"SERPAPI_KEY": "test", "TELEGRAM_BOT_TOKEN": "t",
                                                "TELEGRAM_CHAT_ID": "1", "FDM_NOW": "2026-11-10T05:17:00+00:00"},
                                   clear=False)
        self.env.start()
        for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"):
            os.environ.pop(k, None)
        self.sent = []
        self.tg = mock.patch.object(monitor, "send_telegram", side_effect=lambda t: self.sent.append(t))
        self.tg.start()

    def tearDown(self):
        self.tg.stop()
        self.env.stop()
        self.tmp.cleanup()


class TestPlan(unittest.TestCase):
    def test_combos_cover_window_and_spread(self):
        plans = monitor.all_combos(CFG, dt.date(2026, 11, 1))
        v1 = plans["V1"]
        self.assertEqual(len(v1), 19)
        self.assertEqual(len({c["out"] for c in v1}), 19)
        self.assertTrue(all("2027-03-01" <= c["out"] <= "2027-05-15" for c in v1))
        durs = {(dt.date.fromisoformat(c["ret"]) - dt.date.fromisoformat(c["out"])).days for c in v1}
        self.assertEqual(durs, {27, 30, 33})
        gaps = [abs((dt.date.fromisoformat(a["out"]) - dt.date.fromisoformat(b["out"])).days) for a, b in zip(v1, v1[1:])]
        self.assertGreaterEqual(min(gaps), 8)  # aufeinanderfolgende Suchen liegen auseinander

    def test_past_dates_skipped(self):
        plans = monitor.all_combos(CFG, dt.date(2027, 4, 1))
        self.assertTrue(all(c["out"] > "2027-04-01" for c in plans["V1"]))

    def test_weighted_pattern(self):
        p = monitor.weighted_pattern(CFG["variants"])
        self.assertEqual(len(p), 8)
        self.assertEqual(p.count("V1"), 3)
        self.assertEqual(p.count("V4"), 1)

    def test_rotation_visits_everything(self):
        state, seen = {}, set()
        for _ in range(60):
            for c in monitor.next_combos(CFG, state, 3, dt.date(2026, 11, 1), None):
                seen.add((c["variant"], c["out"]))
        self.assertEqual(len(seen), 19 * 4)


class TestBudget(unittest.TestCase):
    def test_start_of_month(self):
        n, ver, _ = monitor.searches_for_run(CFG, {}, dt.datetime(2026, 11, 1, 5, tzinfo=dt.timezone.utc), 250)
        self.assertEqual(n, 3)
        self.assertEqual(ver, 2)

    def test_exhausted(self):
        n, ver, note = monitor.searches_for_run(CFG, {}, dt.datetime(2026, 11, 20, 5, tzinfo=dt.timezone.utc), 0)
        self.assertEqual((n, ver), (0, 0))
        self.assertIn("erschöpft", note)

    def test_month_total_within_budget(self):
        left, state, total = 250, {}, 0
        for day in range(1, 31):
            for hour in (5, 17):
                n, ver, _ = monitor.searches_for_run(CFG, state, dt.datetime(2026, 11, day, hour, tzinfo=dt.timezone.utc), left)
                spend = n + min(ver * 2, 2)  # Annahme: ein Kandidat pro Lauf (Worst Case)
                spend = min(spend, left)
                left -= spend
                total += spend
        self.assertLessEqual(total, 250)
        self.assertGreaterEqual(left, 0)


class TestEvaluate(unittest.TestCase):
    def offer(self, price, **kw):
        return {"price": price, "leg": monitor.parse_leg(itin(price, **kw))}

    def test_tiers(self):
        e = monitor.evaluate(CFG, self.offer(700), "V1", None)
        self.assertEqual((e["tier"], e["notify"], e["total"]), ("EXTREM GUT", True, 700))
        e = monitor.evaluate(CFG, self.offer(800), "V1", None)
        self.assertEqual((e["tier"], e["notify"]), ("GUT", True))
        e = monitor.evaluate(CFG, self.offer(900), "V1", None)
        self.assertEqual((e["tier"], e["notify"]), ("NORMAL", False))
        e = monitor.evaluate(CFG, self.offer(990), "V1", None)
        self.assertEqual((e["tier"], e["notify"]), ("ZU TEUER", False))

    def test_good_tier_needs_good_connection(self):
        e = monitor.evaluate(CFG, self.offer(800, layover=11 * 60), "V1", None)
        self.assertEqual(e["tier"], "GUT")
        self.assertFalse(e["notify"])
        self.assertTrue(any("langer Umstieg" in i for i in e["issues"]))

    def test_baggage_surcharge_unknown_airline(self):
        e = monitor.evaluate(CFG, self.offer(680, airline="Lufthansa"), "V1", None)
        self.assertEqual(e["total"], 800)
        self.assertIn("Zuschlag", e["bag_note"])

    def test_variant_penalty(self):
        e = monitor.evaluate(CFG, self.offer(730), "V2", None)  # 730 + 40 = 770 effektiv
        self.assertEqual(e["tier"], "GUT")
        self.assertEqual(e["total"], 730)

    def test_below_baseline_rule(self):
        e = monitor.evaluate(CFG, self.offer(860, layover=11 * 60), "V1", 1050)
        self.assertTrue(e["notify"])
        self.assertTrue(any("unter beobachtetem Niveau" in r for r in e["reasons"]))

    def test_self_transfer_never_notified(self):
        e = monitor.evaluate(CFG, self.offer(600, ext=["Self transfer in DOH"]), "V1", None)
        self.assertFalse(e["notify"])

    def test_baseline_needs_samples(self):
        now = dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)
        obs = [{"ts": "2026-11-0%dT05:00:00Z" % i, "variant": "V1", "status": "ok", "best_total": 900 + i} for i in range(1, 5)]
        self.assertIsNone(monitor.baseline(CFG, obs, "V1", now))
        obs += [{"ts": "2026-11-0%dT17:00:00Z" % i, "variant": "V1", "status": "ok", "best_total": 950} for i in range(1, 5)]
        self.assertEqual(monitor.baseline(CFG, obs, "V1", now), 927.0)


class TestBooking(unittest.TestCase):
    def test_free_bag(self):
        b = monitor.parse_booking_options(booking_resp(800))
        self.assertEqual(b["bag_cost"], 0.0)
        self.assertFalse(b["separate_tickets"])

    def test_paid_bag(self):
        b = monitor.parse_booking_options(booking_resp(700, ("1 free carry-on", "1st checked bag: €65")))
        self.assertEqual(b["bag_cost"], 130.0)

    def test_separate_tickets(self):
        b = monitor.parse_booking_options({"booking_options": [{"departing": {"price": 300}, "returning": {"price": 350}}]})
        self.assertTrue(b["separate_tickets"])
        self.assertFalse(b["found"])


class TestDedupeStale(Base):
    def test_stale_notification_does_not_block(self):
        cfgd = CFG
        state = {"notified": {
            "old": {"total": 694.0, "tier": "EXTREM GUT", "ts": "2026-11-09T05:00:00Z", "variant": "V2"},
            "k2": {"total": 700.0, "tier": "EXTREM GUT", "ts": "2026-11-09T05:00:00Z", "variant": "V2",
                   "out": "2027-03-01", "ret": "2027-03-28"}}}
        d = {"key": "new", "variant": "V2", "out": "2027-04-14", "ret": "2027-05-17", "ts": "2026-11-10T05:17:00Z",
             "eval": {"tier": "GUT", "total": 752.0}}
        now = dt.datetime(2026, 11, 10, 5, 17, tzinfo=dt.timezone.utc)
        # gültige frühere EXTREM-Meldung blockiert eine schlechtere GUT-Meldung
        self.assertFalse(monitor.should_notify(cfgd, state, d, now, {("V2", "2027-03-01", "2027-03-28"): 700.0}))
        # wurde der frühere Termin inzwischen teurer gemessen (z. B. Gepäck nicht inklusive) -> blockiert nicht mehr
        self.assertTrue(monitor.should_notify(cfgd, state, d, now, {("V2", "2027-03-01", "2027-03-28"): 824.0}))

    def test_booking_call_uses_full_params(self):
        fake = FakeSerp(690, 690, 690)
        with mock.patch.object(monitor, "http_get_json", fake):
            monitor.run(CFG)
        bcalls = [c for c in fake.calls if "booking_token" in c]
        self.assertTrue(bcalls)
        for c in bcalls:
            self.assertTrue("departure_id" in c or "multi_city_json" in c)
            self.assertNotIn("departure_token", c)


class TestNoBag(Base):
    def test_booking_says_no_checked_bag(self):
        fake = FakeSerp(690, 690, 690, bags=["1 free carry-on", "No checked bags"])
        with mock.patch.object(monitor, "http_get_json", fake):
            monitor.run(CFG)
        deals = json.loads((monitor.DATA / "deals.json").read_text())
        d = [x for x in deals if x["offer"].get("booking")][0]
        self.assertEqual(d["eval"]["bag_cost"], 120.0)
        self.assertEqual(d["eval"]["total"], 810.0)
        self.assertIn("NICHT inklusive", d["eval"]["bag_note"])


class TestRun(Base):
    def test_full_run_notifies_once_and_dedupes(self):
        fake = FakeSerp(first_price=690, ret_price=705, book_price=705)
        with mock.patch.object(monitor, "http_get_json", fake):
            rec = monitor.run(CFG)
        self.assertEqual(rec["status"], "ok")
        n, _, _ = monitor.searches_for_run(CFG, {}, monitor.now_utc(), 200)
        self.assertEqual(rec["searches"], n)
        self.assertEqual(rec["verifications"], 4)  # 2 Kandidaten x (Rückflug + Buchungsoptionen)
        self.assertEqual(len(self.sent), 1, "alle Deals eines Laufs in einer Nachricht")
        deals = json.loads((monitor.DATA / "deals.json").read_text())
        notified = [d["variant"] for d in deals if d["notified"]]
        self.assertGreaterEqual(len(notified), 1)
        self.assertEqual(len(notified), len(set(notified)), "pro Variante nur der beste Deal")
        msg = self.sent[0]
        for needle in ("EXTREM GUT", "705 €", "Qatar Airways", "Hinflug", "Rückflug", "QR 70", "QR 963",
                       "Gepäck: inklusive (laut Buchungsoption verifiziert)", "Google Flights", "Umstieg"):
            self.assertIn(needle, msg)
        params = fake.calls[1]
        self.assertEqual(params["currency"], "EUR")
        self.assertEqual(params["stops"], 2)
        self.assertTrue((monitor.DOCS / "index.html").exists())
        # Zweiter Lauf, gleiche Preise -> keine erneute Meldung für bereits gemeldete Varianten
        first = set(notified)
        os.environ["FDM_NOW"] = "2026-11-10T17:17:00+00:00"
        with mock.patch.object(monitor, "http_get_json", FakeSerp(690, 705, 705)):
            rec2 = monitor.run(CFG)
        deals = json.loads((monitor.DATA / "deals.json").read_text())
        second = [d for d in deals if d["ts"] == rec2["ts"]]
        self.assertTrue(second)
        self.assertFalse([d for d in second if d["notified"] and d["variant"] in first])
        self.assertTrue([d for d in second if d.get("duplicate") and d["variant"] in first])
        # Dritter Lauf: >= 25 € günstiger -> erneute Meldung
        n_before = len(self.sent)
        os.environ["FDM_NOW"] = "2026-11-11T05:17:00+00:00"
        with mock.patch.object(monitor, "http_get_json", FakeSerp(650, 660, 660)):
            monitor.run(CFG)
        self.assertEqual(len(self.sent), n_before + 1)

    def test_expensive_no_notification(self):
        with mock.patch.object(monitor, "http_get_json", FakeSerp(first_price=1020)):
            rec = monitor.run(CFG)
        self.assertEqual(self.sent, [])
        self.assertEqual(rec["verifications"], 0)
        obs = monitor.read_observations()
        self.assertTrue(all(o["tier"] == "ZU TEUER" for o in obs))

    def test_failures_alert_once(self):
        for i in range(4):
            os.environ["FDM_NOW"] = f"2026-11-1{i}T05:17:00+00:00"
            with mock.patch.object(monitor, "http_get_json", FakeSerp(fail=True)):
                rec = monitor.run(CFG)
            self.assertEqual(rec["status"], "error")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("3 Läufe in Folge", self.sent[0])
        runs = json.loads((monitor.DATA / "runs.json").read_text())
        self.assertIn("HTTP 503", runs[-1]["errors"][0])

    def test_missing_key(self):
        os.environ["SERPAPI_KEY"] = ""
        rec = monitor.run(CFG)
        self.assertEqual(rec["status"], "error")
        self.assertIn("SERPAPI_KEY", rec["errors"][0])
        self.assertTrue((monitor.DOCS / "index.html").exists())

    def test_no_channels_documented(self):
        os.environ.pop("TELEGRAM_BOT_TOKEN")
        with mock.patch.object(monitor, "http_get_json", FakeSerp(690)):
            rec = monitor.run(CFG)
        self.assertTrue(any("Keine Benachrichtigungskanäle" in n for n in rec["notes"]))
        deals = json.loads((monitor.DATA / "deals.json").read_text())
        self.assertEqual(deals[0]["notify_skipped"], "kein Kanal konfiguriert")

    def test_no_results_is_not_error(self):
        def fake(url, params, retries=1):
            if "account" in url:
                return {"total_searches_left": 100}
            return {"error": "Google Flights hasn't returned any results for this query."}
        with mock.patch.object(monitor, "http_get_json", fake):
            rec = monitor.run(CFG)
        self.assertEqual(rec["status"], "ok")
        self.assertTrue(all(r["status"] == "no_results" for r in rec["results"]))

    def test_multicity_params(self):
        p = monitor.base_params(CFG, {"variant": "V1", "out": "2027-03-09", "ret": "2027-04-08"}, "k")
        self.assertEqual(p["type"], 3)
        legs = json.loads(p["multi_city_json"])
        self.assertEqual([(l["departure_id"], l["arrival_id"]) for l in legs], [("FRA", "SUB"), ("DPS", "FRA")])
        p = monitor.base_params(CFG, {"variant": "V2", "out": "2027-03-09", "ret": "2027-04-08"}, "k")
        self.assertEqual((p["type"], p["arrival_id"], p["return_date"]), (1, "DPS", "2027-04-08"))


class TestDiscord(Base):
    def setUp(self):
        super().setUp()
        os.environ.pop("TELEGRAM_BOT_TOKEN")
        self.calls = []
        self.dc = mock.patch.object(monitor, "_discord_request",
                                    side_effect=lambda m, u, p, bot: self.calls.append((m, u, p, bot)) or {"id": "dm42"})
        self.dc.start()

    def tearDown(self):
        self.dc.stop()
        for k in ("DISCORD_WEBHOOK_URL", "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_USER_ID", "DISCORD_MENTION_USER_ID"):
            os.environ.pop(k, None)
        super().tearDown()

    def test_webhook_deal_embed(self):
        os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/1/abc"
        os.environ["DISCORD_MENTION_USER_ID"] = "123"
        with mock.patch.object(monitor, "http_get_json", FakeSerp(690, 705, 705)):
            rec = monitor.run(CFG)
        self.assertEqual(rec["channels"], ["discord"])
        self.assertEqual(len(self.calls), 1)
        m, url, p, bot = self.calls[0]
        self.assertTrue(url.endswith("?wait=true"))
        self.assertFalse(bot)
        self.assertTrue(p["content"].startswith("<@123>"))
        self.assertEqual(p["allowed_mentions"], {"users": ["123"]})
        emb = p["embeds"][0]
        self.assertIn("EXTREM GUT", emb["title"])
        names = [f["name"] for f in emb["fields"]]
        for n in ("🛫 Hinflug", "🛬 Rückflug", "💶 Flugpreis", "🧳 Gepäck", "🔗 Links"):
            self.assertIn(n, names)
        hin = emb["fields"][0]["value"]
        self.assertIn("FRA → SUB", hin)
        self.assertIn("2 Flüge", hin)
        self.assertIn("`QR 70`", hin)
        self.assertLessEqual(len(json.dumps(p["embeds"], ensure_ascii=False)), 6000)
        self.assertEqual(emb["url"], "https://www.google.com/travel/flights?test=1")

    def test_bot_dm(self):
        os.environ["DISCORD_BOT_TOKEN"] = "tok"
        os.environ["DISCORD_USER_ID"] = "999"
        sent, errs = monitor.notify("Test", "<b>hallo</b>")
        self.assertEqual((sent, errs), (["discord"], []))
        self.assertIn("/users/@me/channels", self.calls[0][1])
        self.assertIn("/channels/dm42/messages", self.calls[1][1])
        self.assertTrue(self.calls[1][3])
        self.assertEqual(self.calls[1][2]["embeds"][0]["description"], "**hallo**")

    def test_discord_error_reported(self):
        os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/1/abc"
        self.dc.stop()
        with mock.patch.object(monitor, "_discord_request", side_effect=monitor.SourceError("Discord HTTP 401: Unauthorized")):
            sent, errs = monitor.notify("Test", "x")
        self.dc.start()
        self.assertEqual(sent, [])
        self.assertIn("401", errs[0])

    def test_large_messages_split(self):
        os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/1/abc"
        big = [{"description": "x" * 3000} for _ in range(3)]
        monitor.send_discord("S", "", big)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[1][2]["content"], "")


class TestTrips(Base):
    def test_trips_and_budget_shares(self):
        cfg = full_cfg()
        ids = [t["id"] for t in monitor.trips(cfg)]
        self.assertEqual(ids, ["ID", "C", "PH", "LF", "D", "VN", "TH"])
        from collections import Counter
        cnt, st = Counter(), {}
        for _ in range(90):
            for c in monitor.next_combos(cfg, st, 2, dt.date(2026, 11, 5), None):
                cnt[monitor.trip_of(cfg, c["variant"])["id"]] += 1
        # Anteile 5:4:3:2:2:1:1 (Priorität)
        self.assertEqual(cnt, Counter({"PH": 50, "LF": 40, "D": 30, "VN": 20, "TH": 20, "C": 10, "ID": 10}))
        # pausierte Routen (weight 0) werden nie gesucht
        st, seen = {}, set()
        for _ in range(200):
            for c in monitor.next_combos(cfg, st, 3, dt.date(2026, 11, 5), None):
                seen.add(c["variant"])
        self.assertFalse(seen & {"V4", "C2", "D3"})

    def test_hot_trip_gets_double_share(self):
        cfg = full_cfg()
        monitor.trips(cfg)
        cfg["_hot"] = {"TH"}
        from collections import Counter
        cnt, st = Counter(), {}
        for _ in range(110):
            for c in monitor.next_combos(cfg, st, 2, dt.date(2026, 11, 5), None):
                cnt[monitor.trip_of(cfg, c["variant"])["id"]] += 1
        self.assertEqual(cnt["TH"], 2 * cnt["VN"])
        obs = [{"ts": "2026-11-09T05:00:00Z", "variant": "LF1", "tier": "GUT"},
               {"ts": "2026-10-01T05:00:00Z", "variant": "VN1", "tier": "EXTREM GUT"}]
        self.assertEqual(monitor.hot_trips(cfg, obs, dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)), {"LF"})

    def test_all_fixed_windows_visited(self):
        cfg, st, seen = full_cfg(), {}, set()
        for _ in range(60):
            for c in monitor.next_combos(cfg, st, 4, dt.date(2026, 11, 5), None):
                if monitor.trip_of(cfg, c["variant"])["id"] != "ID":
                    seen.add((c["variant"], c["out"], c["ret"]))
        active = [v for t in monitor.trips(cfg)[1:] for v in t["variants"] if int(v.get("weight", 1)) > 0]
        self.assertEqual(len(seen), 3 * len(active))
        self.assertIn(("PH1", "2027-03-20", "2027-04-19"), seen)

    def test_past_windows_dropped(self):
        plans = monitor.all_combos(full_cfg(), dt.date(2027, 4, 2))
        self.assertEqual([c["out"] for c in plans["PH1"]], ["2027-04-15"])

    def test_cursor_migration_keeps_progress(self):
        cfg = full_cfg()
        st = {"cursor": {"pattern": 5, "variant": {"V1": 7}}}
        monitor.next_combos(cfg, st, 1, dt.date(2026, 11, 5), None)
        self.assertIn("trips", st["cursor"])
        self.assertGreaterEqual(st["cursor"]["trips"]["ID"]["variant"].get("V1", 7), 7)

    def test_params_new_trip(self):
        p = monitor.base_params(full_cfg(), {"variant": "TH1", "out": "2027-04-05", "ret": "2027-05-05"}, "k")
        self.assertEqual((p["type"], p["departure_id"], p["arrival_id"], p["stops"]), (1, "FRA,DUS,MUC,BER", "BKK", 3))
        p = monitor.base_params(full_cfg(), {"variant": "PH2", "out": "2027-04-01", "ret": "2027-05-01"}, "k")
        self.assertEqual((p["type"], p["arrival_id"]), (1, "CEB,DGT,MNL"))
        p = monitor.base_params(full_cfg(), {"variant": "LF2", "out": "2027-05-01", "ret": "2027-05-31"}, "k")
        legs = json.loads(p["multi_city_json"])
        self.assertEqual((legs[0]["arrival_id"], legs[1]["departure_id"]), ("LOP,DPS", "LBJ,DPS"))
        p = monitor.base_params(CFG, {"variant": "V2", "out": "2027-04-05", "ret": "2027-05-05"}, "k")
        self.assertEqual((p["departure_id"], p["stops"]), ("FRA", 2))

    def offer(self, price, typical=None, **kw):
        return {"price": price, "leg": monitor.parse_leg(itin(price, **kw)), "typical": typical}

    def test_relative_threshold(self):
        cfg = full_cfg()
        e = monitor.evaluate(cfg, self.offer(780, [900, 1100]), "TH1", None)   # 22 % unter 1000
        self.assertEqual((e["tier"], e["notify"]), ("GUT", True))
        e = monitor.evaluate(cfg, self.offer(850, [900, 1100]), "TH1", None)   # 15 %
        self.assertFalse(e["notify"])
        e = monitor.evaluate(cfg, self.offer(650, [900, 1100]), "TH1", None)   # 35 %
        self.assertEqual(e["tier"], "EXTREM GUT")
        e = monitor.evaluate(cfg, self.offer(780, [900, 1100], layover=12 * 60), "TH1", None)  # schlechte Verbindung
        self.assertFalse(e["notify"])
        e = monitor.evaluate(cfg, self.offer(680, [900, 1100], layover=12 * 60), "TH1", None)  # 32 % reicht
        self.assertTrue(e["notify"])
        e = monitor.evaluate(cfg, self.offer(500), "TH1", None)                # kein Vergleichspreis
        self.assertFalse(e["notify"])
        self.assertTrue(any("Vergleichspreis" in i for i in e["issues"]))

    def test_absolute_for_scenario_c(self):
        e = monitor.evaluate(full_cfg(), self.offer(700), "C1", None)
        self.assertEqual((e["tier"], e["notify"]), ("EXTREM GUT", True))

    def test_access_cost_for_munich(self):
        cfg = full_cfg()
        o = self.offer(700)
        o["leg"]["segments"][0]["from"] = "MUC"
        e = monitor.evaluate(cfg, o, "C1", None)
        self.assertEqual(e["total"], 700)
        self.assertEqual(e["effective"], 740)
        self.assertIn("MUC", e["access_notes"][0])
        o["return_leg"] = monitor.parse_leg(itin(1))
        o["return_leg"]["segments"][-1]["to"] = "MUC"
        e = monitor.evaluate(cfg, o, "C1", None)
        self.assertEqual(e["effective"], 780)
        self.assertEqual(e["tier"], "GUT")

    def test_separate_tickets_allowed_with_warning(self):
        cfg = full_cfg()
        e = monitor.evaluate(cfg, self.offer(600, ext=["Separate tickets booked together"]), "C1", None)
        self.assertTrue(e["notify"])
        self.assertTrue(any("kein Anschluss-Schutz" in i for i in e["issues"]))
        e = monitor.evaluate(CFG, self.offer(600, ext=["Separate tickets booked together"]), "V1", None)
        self.assertFalse(e["notify"])

    def test_booking_options_separate(self):
        data = {"booking_options": [
            {"together": {"book_with": "Expedia", "price": 500, "option_title": "Separate tickets booked together",
                          "baggage_prices": []}},
            {"together": {"book_with": "Qatar Airways", "price": 640, "baggage_prices": ["1st checked bag free"]}}]}
        b = monitor.parse_booking_options(data, allow_separate=False)
        self.assertEqual((b["book_with"], b["separate_tickets"]), ("Qatar Airways", False))
        b = monitor.parse_booking_options(data, allow_separate=True)
        self.assertEqual((b["book_with"], b["separate_tickets"]), ("Expedia", True))

    def test_ground_costs(self):
        g = monitor.ground_for(full_cfg(), "D1")
        self.assertEqual((g["min"], g["max"]), (9 + 60 + 9 + 40 + 4 + 11, 60 + 160 + 13 + 60 + 25 + 45))
        self.assertEqual(g["open"], [])
        g = monitor.ground_for(full_cfg(), "PH1")
        self.assertIn("Apo Island Tagesausflüge", g["open"])

    def test_rank_score(self):
        cfg = full_cfg()
        o = self.offer(700, [900, 1100])
        e = monitor.evaluate(cfg, o, "TH1", None)
        g = monitor.ground_for(cfg, "TH1")
        self.assertAlmostEqual(e["score"], e["effective"] + (g["min"] + g["max"]) / 2, places=1)
        o2 = self.offer(700, [900, 1100], stops=2)
        o2["leg"]["duration_min"] = 26 * 60
        e2 = monitor.evaluate(cfg, o2, "TH1", None)
        self.assertAlmostEqual(e2["score"] - e["score"], 40 + 60, places=1)

    def test_prefer_airline_direct(self):
        data = {"booking_options": [
            {"together": {"book_with": "lastminute.com", "price": 637, "baggage_prices": []}},
            {"together": {"book_with": "Etihad", "price": 655, "baggage_prices": ["1st checked bag free"]}}]}
        b = monitor.parse_booking_options(data, False, ["Etihad"])
        self.assertEqual((b["book_with"], b["direct"], b["cheapest_other"]["price"]), ("Etihad", True, 637))
        data["booking_options"][1]["together"]["price"] = 704
        b = monitor.parse_booking_options(data, False, ["Etihad"])
        self.assertEqual(b["book_with"], "lastminute.com")

    def test_full_run_new_trip_with_discord(self):
        cfg = full_cfg()
        os.environ.pop("TELEGRAM_BOT_TOKEN")
        os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/1/abc"
        calls = []
        try:
            with mock.patch.object(monitor, "_discord_request", side_effect=lambda m, u, p, bot: calls.append(p) or {}), \
                    mock.patch.object(monitor, "http_get_json", FakeSerp(560, 560, 560, typical=(900, 1100))):
                # mehrere Läufe, damit auch Szenarien A/B/D an die Reihe kommen
                for i in range(4):
                    os.environ["FDM_NOW"] = f"2026-11-1{i}T05:17:00+00:00"
                    monitor.run(cfg)
        finally:
            os.environ.pop("DISCORD_WEBHOOK_URL")
        self.assertTrue(calls)
        titles = [e["title"] for p in calls for e in p["embeds"]]
        flags = ("🇹🇭", "🇵🇭", "🇲🇾", "🇻🇳", "🦎")
        self.assertTrue(any(any(f in t for f in flags) for t in titles), titles)
        emb = next(e for p in calls for e in p["embeds"] if any(f in e["title"] for f in flags))
        names = [f["name"] for f in emb["fields"]]
        self.assertIn("🚤 Inland, Fähren & Transfers (Richtwerte)", names)
        self.assertIn("🌦️ Saison", names)
        for p in calls:
            self.assertLessEqual(len(json.dumps(p["embeds"], ensure_ascii=False)), 6000)
        self.assertTrue((monitor.DOCS / "index.html").exists())

    def test_multi_origin_fallback(self):
        cfg = full_cfg()
        fake = FakeSerp(1500, typical=(900, 1100), reject_multi_origin=True)
        with mock.patch.object(monitor, "http_get_json", fake):
            for i in range(3):
                os.environ["FDM_NOW"] = f"2026-11-1{i}T05:17:00+00:00"
                rec = monitor.run(cfg)
        st = json.loads((monitor.DATA / "state.json").read_text())
        self.assertTrue(st.get("mc_single_origin"))
        mc = [c for c in fake.calls if c.get("type") == 3]
        self.assertTrue(any("FRA,DUS" in c["multi_city_json"] for c in mc))
        self.assertTrue(any('"departure_id":"FRA"' in c["multi_city_json"] for c in mc))
        runs = json.loads((monitor.DATA / "runs.json").read_text())
        self.assertTrue(any("nur ab FRA" in n for r in runs for n in r["notes"]))
        self.assertNotEqual(rec["status"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=1)
