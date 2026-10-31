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

CFG = monitor.load_config(ROOT / "config.toml")


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

    def __init__(self, first_price=690, ret_price=None, book_price=None, fail=False, bags=None):
        self.first, self.ret = first_price, ret_price or first_price
        self.book = book_price or self.ret
        self.fail, self.bags = fail, bags
        self.calls = []

    def __call__(self, url, params, retries=1):
        self.calls.append(dict(params))
        if "account" in url:
            return {"total_searches_left": 200}
        if self.fail:
            raise monitor.SourceError("HTTP 503: Service unavailable")
        if "booking_token" in params:
            return booking_resp(self.book, self.bags) if self.bags is not None else booking_resp(self.book)
        if "departure_token" in params:
            return return_resp(self.ret)
        return search_resp([itin(self.first), itin(self.first + 180, airline="Lufthansa")])


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
        for n in ("🛫 Hinflug", "🛬 Rückflug", "💶 Preis", "🧳 Gepäck", "🔗 Links"):
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
