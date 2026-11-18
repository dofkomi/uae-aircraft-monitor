"""اختبارات وحدة بسيطة (تستخدم unittest من المكتبة القياسية)."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from adsb_sources import BaseSource
from airports import Airport
from classifier import CIVILIAN, MILITARY, classify, is_uae_related
from models import AircraftState, SignalEvent
from store import Store
from tracker import AircraftTracker


class DummyDB:
    async def enrich(self, state: AircraftState) -> None:
        pass  # لا نريد طلبات شبكة أثناء الاختبار


class FakeSource(BaseSource):
    def __init__(self, states: list[AircraftState]) -> None:
        super().__init__()
        self.states = states

    async def fetch(self) -> list[AircraftState]:
        self._mark_ok(len(self.states))
        return list(self.states)


class FakeGeo:
    async def lookup(self, lat, lon):
        return ("العراق", "IQ", "nominatim")


class FakeAirports:
    AIRPORTS = [
        Airport("LIRF", "FCO", "روما فيوميتشينو", 41.80, 12.25, "IT", "large_airport"),
        Airport("FTTJ", "NDJ", "نجامينا", 12.13, 15.04, "TD", "medium_airport"),
        Airport("OMDB", "DXB", "دبي", 25.25, 55.36, "AE", "large_airport"),
    ]

    def nearest(self, lat, lon, max_km=25.0):
        from airports import haversine_km

        matches = [a for a in self.AIRPORTS if haversine_km(lat, lon, a.lat, a.lon) <= max_km]
        return matches[0] if matches else None


class TrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        config.settings.db_path = Path(self.tmp.name) / "test.db"
        config.settings.signal_loss_minutes = 5
        self.store = Store(config.settings.db_path)
        self.db = DummyDB()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_signal_loss_is_detected_once(self) -> None:
        old = int((datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp())
        state = AircraftState(
            icao24="abc123",
            callsign="TEST1",
            latitude=24.5,
            longitude=54.0,
            aircraft_type="F16",
            last_contact=old,
        )
        source = FakeSource([state])
        tracker = AircraftTracker([source], self.db, self.store)

        asyncio.run(tracker.poll())
        self.assertIn("abc123", tracker.current)
        self.assertEqual(tracker.classifications["abc123"], MILITARY)
        self.assertEqual(tracker.recent_events, [])

        # الطائرة تختفي من كل المصادر
        source.states = []
        asyncio.run(tracker.poll())
        self.assertEqual(len(tracker.recent_events), 1)
        event = tracker.recent_events[0]
        self.assertEqual(event.icao24, "abc123")
        self.assertEqual(event.callsign, "TEST1")
        self.assertEqual(event.reason, "no_broadcast")
        self.assertEqual(event.classification, MILITARY)

        # لا نكرّر نفس الحدث في كل دورة جلب
        asyncio.run(tracker.poll())
        self.assertEqual(len(tracker.recent_events), 1)

    def test_aircraft_reappearing_resets_alert(self) -> None:
        old = int((datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp())
        state = AircraftState(icao24="def456", callsign="TEST2", latitude=24.5,
                              longitude=54.0, last_contact=old)
        source = FakeSource([state])
        tracker = AircraftTracker([source], self.db, self.store)

        asyncio.run(tracker.poll())
        source.states = []
        asyncio.run(tracker.poll())
        self.assertEqual(len(tracker.recent_events), 1)

        source.states = [state]
        asyncio.run(tracker.poll())
        self.assertNotIn("def456", tracker.reported_missing)
        self.assertFalse(tracker.reported_missing)


    def test_signal_loss_records_country(self) -> None:
        old = int((datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp())
        state = AircraftState(icao24="geo001", callsign="UAE900", latitude=33.3,
                              longitude=44.4, last_contact=old)
        source = FakeSource([state])
        tracker = AircraftTracker([source], self.db, self.store, geo=FakeGeo())

        asyncio.run(tracker.poll())
        source.states = []
        asyncio.run(tracker.poll())
        event = tracker.recent_events[0]
        self.assertEqual(event.country, "العراق")
        self.assertEqual(event.country_code, "IQ")
        self.assertEqual(event.geo_source, "nominatim")

    def test_foreign_landing_then_non_uae_destination(self) -> None:
        # طائرة إماراتية: تقلع ثم تهبط في روما، ثم تقلع وتهبط في نجامينا.
        callsign = "UAE777"
        before_rome = AircraftState(
            icao24="uae001", callsign=callsign, registration="A6-XXX",
            latitude=41.9, longitude=12.4, baro_altitude=3000.0, velocity=200.0,
        )
        at_rome = AircraftState(
            icao24="uae001", callsign=callsign, registration="A6-XXX",
            latitude=41.80, longitude=12.25, on_ground=True,
            baro_altitude=0.0, velocity=0.0,
        )
        en_route = AircraftState(
            icao24="uae001", callsign=callsign, registration="A6-XXX",
            latitude=25.0, longitude=20.0,
            baro_altitude=10000.0, velocity=230.0,
        )
        at_ndjamena = AircraftState(
            icao24="uae001", callsign=callsign, registration="A6-XXX",
            latitude=12.13, longitude=15.04, on_ground=True,
            baro_altitude=0.0, velocity=0.0,
        )
        source = FakeSource([before_rome])
        tracker = AircraftTracker([source], self.db, self.store, airports=FakeAirports())

        asyncio.run(tracker.poll())          # محلّقة قبل روما
        source.states = [at_rome]
        asyncio.run(tracker.poll())          # هبوط روما
        source.states = [en_route]
        asyncio.run(tracker.poll())          # إقلاع ومتابعة
        source.states = [at_ndjamena]
        asyncio.run(tracker.poll())          # هبوط نجامينا

        landings = tracker.recent_landings
        self.assertEqual(len(landings), 2)
        self.assertEqual(landings[0].airport_ident, "LIRF")
        self.assertEqual(landings[1].airport_ident, "FTTJ")
        self.assertEqual(landings[1].from_airport, "LIRF")
        self.assertTrue(landings[1].divergence)
        self.assertTrue(landings[1].uae_related)
        self.assertIsNotNone(landings[1].leg_distance_km)
        self.assertIsNotNone(landings[1].leg_minutes)

    def test_parked_aircraft_not_reported_as_landing(self) -> None:
        """طائرة متوقفة على الأرض عند أول تشغيل لا تُعدّ «هبوطاً»."""
        parked = AircraftState(
            icao24="park01", callsign="PARK1", latitude=25.25, longitude=55.36,
            on_ground=True, baro_altitude=0.0, velocity=0.0,
        )
        source = FakeSource([parked])
        tracker = AircraftTracker([source], self.db, self.store, airports=FakeAirports())
        asyncio.run(tracker.poll())
        self.assertEqual(tracker.recent_landings, [])

    def test_report_contains_detailed_sections(self) -> None:
        from reporter import build_report

        state = AircraftState(
            icao24="rep001", callsign="UAE123", registration="A6-ABC",
            aircraft_type="B738", latitude=25.0, longitude=55.0,
            baro_altitude=10000.0, velocity=230.0, true_track=315.0,
            vertical_rate=5.0, squawk="7700", origin_country="United Arab Emirates",
        )
        source = FakeSource([state])
        tracker = AircraftTracker(
            [source], self.db, self.store, geo=FakeGeo(), airports=FakeAirports()
        )
        asyncio.run(tracker.poll())
        text = build_report(tracker)
        for needle in (
            "الملخص التنفيذي",
            "UAE123",
            "A6-ABC",
            "B738",
            "أكواد طوارئ",
            "الطائرات المرتبطة بالإمارات",
            "صعود",
            "7700",
        ):
            self.assertIn(needle, text)

    def test_parked_aircraft_shown_as_on_ground(self) -> None:
        from reporter import build_report

        parked = AircraftState(
            icao24="park02", callsign="A6-PRK", registration="A6-PRK",
            latitude=25.25, longitude=55.36, on_ground=True,
            baro_altitude=0.0, velocity=0.0,
        )
        source = FakeSource([parked])
        tracker = AircraftTracker([source], self.db, self.store, airports=FakeAirports())
        asyncio.run(tracker.poll())
        text = build_report(tracker)
        self.assertIn("على الأرض", text)


class AdsbParsingTests(unittest.TestCase):
    def test_ground_altitude_marks_on_ground(self) -> None:
        from adsb_sources import _alt_to_m, _ground_flag

        self.assertTrue(_ground_flag({"alt_baro": "ground"}, "ground"))
        self.assertEqual(_alt_to_m("ground"), 0.0)

    def test_airborne_altitude_not_on_ground(self) -> None:
        from adsb_sources import _ground_flag

        self.assertFalse(_ground_flag({"alt_baro": 30000}, 30000))
        self.assertFalse(_ground_flag({}, None))


class SecurityTests(unittest.TestCase):
    """البوت خاص: قائمة سماح صارمة + إخفاء التوكن من السجلات."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        config.settings.db_path = Path(self.tmp.name) / "test.db"
        self._original_ids = config.settings.telegram_chat_ids

    def tearDown(self) -> None:
        config.settings.telegram_chat_ids = self._original_ids
        self.tmp.cleanup()

    def _bot(self):
        from bot import ReportBot

        bot = ReportBot(None)  # لا نحتاج متتبّعاً لاختبار الصلاحيات
        bot.dynamic_chats.clear()
        return bot

    def test_allowlist_accepts_registered_chat_only(self) -> None:
        config.settings.telegram_chat_ids = (111,)
        bot = self._bot()
        self.assertTrue(bot.is_allowed(111))
        self.assertFalse(bot.is_allowed(222))

    def test_empty_allowlist_rejects_everyone(self) -> None:
        # fail-closed: لا «السماح للجميع» عند غياب الإعداد
        config.settings.telegram_chat_ids = ()
        bot = self._bot()
        self.assertFalse(bot.is_allowed(111))

    def test_dynamic_chat_can_be_added_by_owner(self) -> None:
        config.settings.telegram_chat_ids = (111,)
        bot = self._bot()
        bot.dynamic_chats.add(333)
        self.assertTrue(bot.is_allowed(333))

    def test_token_is_redacted_from_logs(self) -> None:
        import logging as logging_module

        from secure_logging import RedactSecretsFilter

        record = logging_module.LogRecord(
            "httpx",
            logging_module.INFO,
            __file__,
            1,
            "HTTP Request: POST https://api.telegram.org/"
            "bot123456789:AAFakeTokenValue1234567890/getMe",
            None,
            None,
        )
        RedactSecretsFilter().filter(record)
        text = record.getMessage()
        self.assertNotIn("AAFakeTokenValue", text)
        self.assertIn("<محجوب>", text)


class MigrationTests(unittest.TestCase):
    def test_legacy_schema_gets_missing_columns(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE signal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                icao24 TEXT NOT NULL, callsign TEXT, classification TEXT,
                last_seen INTEGER NOT NULL, lat REAL, lon REAL, reason TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE landings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                icao24 TEXT NOT NULL, callsign TEXT, registration TEXT,
                classification TEXT, uae_related INTEGER, airport_ident TEXT,
                airport_name TEXT, airport_country TEXT, at INTEGER NOT NULL,
                lat REAL, lon REAL, from_airport TEXT, from_country TEXT,
                divergence INTEGER
            );
            """
        )
        conn.commit()
        conn.close()

        store = Store(path)
        check = sqlite3.connect(path)
        try:
            columns = {
                row[1] for row in check.execute("PRAGMA table_info(signal_events)")
            }
        finally:
            check.close()
        self.assertIn("country", columns)
        self.assertIn("nearest_airport", columns)

        # يجب أن يعمل الإدراج بعد الترقية (كان يفشل سابقاً)
        store.add_event(
            SignalEvent(
                icao24="abc123",
                callsign="TST1",
                classification="civilian",
                last_seen=datetime.now(timezone.utc),
                last_lat=24.0,
                last_lon=54.0,
                country="الإمارات",
                country_code="AE",
                nearest_airport="OMDB",
                geo_source="nominatim",
            )
        )
        self.assertEqual(store.stats()["signal_events"], 1)
        tmp.cleanup()


class ClassifierTests(unittest.TestCase):
    def test_military_type(self) -> None:
        state = AircraftState(icao24="a1", aircraft_type="F16C")
        self.assertEqual(classify(state), MILITARY)

    def test_civil_helicopter_not_military(self) -> None:
        # EC130 يحوي "C130" لكنه مروحية مدنية — كان سبب خطأ سابقاً
        state = AircraftState(icao24="a2", aircraft_type="EC130", registration="A6-FTK")
        self.assertEqual(classify(state), CIVILIAN)

    def test_air_france_callsign_not_military(self) -> None:
        state = AircraftState(icao24="a3", callsign="AFR123")
        self.assertNotEqual(classify(state), MILITARY)

    def test_uae_related_detection(self) -> None:
        state = AircraftState(icao24="a4", callsign="XYZ123", registration="A6-ABC")
        self.assertTrue(is_uae_related(state))
        state2 = AircraftState(icao24="a5", callsign="ETD456")
        self.assertTrue(is_uae_related(state2))
        state3 = AircraftState(icao24="a6", callsign="AFR123", registration="F-GABC")
        self.assertFalse(is_uae_related(state3))


class CountryShapeTests(unittest.TestCase):
    """تحديد الدولة من الإحداثيات محلياً (Natural Earth) بلا شبكة."""

    @classmethod
    def setUpClass(cls) -> None:
        from country_shapes import CountryShapes

        path = Path(__file__).resolve().parents[1] / "data" / "countries.geojson"
        cls.shapes = CountryShapes(cache_path=path)
        if not cls.shapes.ready:
            raise unittest.SkipTest("data/countries.geojson غير موجود")

    def test_united_arab_emirates(self) -> None:
        name, code, source = self.shapes.lookup(24.45, 54.37)  # أبوظبي
        self.assertEqual(code, "AE")
        self.assertEqual(source, "offline")
        self.assertIn("الإمارات", name or "")

    def test_neighbours(self) -> None:
        self.assertEqual(self.shapes.lookup(23.60, 58.50)[1], "OM")  # مسقط
        self.assertEqual(self.shapes.lookup(24.70, 46.70)[1], "SA")  # الرياض
        self.assertEqual(self.shapes.lookup(35.70, 51.40)[1], "IR")  # طهران

    def test_open_sea_has_no_country(self) -> None:
        # بحر العرب — بعيد عن أي حدود برية
        name, code, source = self.shapes.lookup(15.0, 65.0)
        self.assertIsNone(name)
        self.assertIsNone(code)
        self.assertEqual(source, "unknown")

    def test_label_and_flag(self) -> None:
        from country_shapes import flag_emoji

        self.assertEqual(flag_emoji("AE"), "🇦🇪")
        self.assertEqual(flag_emoji(""), "")
        self.assertIn("الإمارات", self.shapes.country_label(24.45, 54.37))
        self.assertIn("المياه", self.shapes.country_label(15.0, 65.0))

    def test_geo_resolver_prefers_offline_without_network(self) -> None:
        from geo import GeoResolver

        tmp = tempfile.TemporaryDirectory()
        store = Store(Path(tmp.name) / "geo.db")
        # enabled=False يمنع Nominatim؛ airports=None لن يُستخدم لأن التحديد المحلي ينجح
        resolver = GeoResolver(store=store, airports=None, enabled=False, shapes=self.shapes)
        country, code, source = asyncio.run(resolver.lookup(24.45, 54.37))
        self.assertEqual(code, "AE")
        self.assertEqual(source, "offline")
        # النتيجة تُخزَّن مؤقتاً في قاعدة البيانات
        key = f"{24.45:.1f},{54.37:.1f}"
        self.assertIsNotNone(store.get_geo(key))
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
