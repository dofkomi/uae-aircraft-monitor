"""اختبارات الميزات الجديدة: تنبؤ العسكرية، الوجهات غير المبرمجة، PDF/DOCX."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from adsb_sources import BaseSource
from airports import Airport
from classifier import assess, classify, predict_military
from models import AircraftState
from store import Store
from tracker import AircraftTracker


class DummyDB:
    async def enrich(self, state: AircraftState) -> None:
        pass


class FakeSource(BaseSource):
    def __init__(self, states: list[AircraftState]) -> None:
        super().__init__()
        self.states = states

    async def fetch(self) -> list[AircraftState]:
        self._mark_ok(len(self.states))
        return list(self.states)


class FakeAirports:
    AIRPORTS = [
        Airport("LIRF", "FCO", "روما", 41.80, 12.25, "IT", "large_airport"),
        Airport("FTTJ", "NDJ", "نجامينا", 12.13, 15.04, "TD", "medium_airport"),
        Airport("OMDB", "DXB", "دبي", 25.25, 55.36, "AE", "large_airport"),
    ]

    def nearest(self, lat, lon, max_km=25.0):
        from airports import haversine_km

        matches = [
            a for a in self.AIRPORTS if haversine_km(lat, lon, a.lat, a.lon) <= max_km
        ]
        return matches[0] if matches else None

    def by_ident(self, ident):
        return next((a for a in self.AIRPORTS if a.ident == ident), None)


class MilitaryPredictionTests(unittest.TestCase):
    def test_known_fighter_is_confident_military(self) -> None:
        state = AircraftState(icao24="mil001", callsign="RASHD1", aircraft_type="F16")
        result = assess(state)
        self.assertGreaterEqual(result["score"], 0.8)
        self.assertEqual(result["verdict"], "military")
        self.assertTrue(result["reasons"])

    def test_airliner_is_civilian(self) -> None:
        state = AircraftState(
            icao24="civ001", callsign="UAE123", registration="A6-ABC",
            aircraft_type="B738",
        )
        result = assess(state)
        self.assertLess(result["score"], 0.2)
        self.assertIn(result["verdict"], ("civilian", "likely_civilian"))

    def test_hex_only_low_fast_jet_raises_suspicion(self) -> None:
        """بلا نداء ولا تسجيل، منخفض جداً وسريع: مؤشرات عسكرية محتملة."""
        state = AircraftState(
            icao24="hex777", aircraft_type=None, latitude=24.0, longitude=54.0,
            baro_altitude=120.0, velocity=250.0,
        )
        is_military, score, reasons = predict_military(state)
        self.assertGreaterEqual(score, 0.4)
        self.assertTrue(reasons)
        self.assertIn(classify(state), ("unknown", "military"))


class UnplannedDestinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.store = Store(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _land(self, tracker, source, lat, lon, icao="uae900") -> None:
        state = AircraftState(
            icao24=icao, callsign="UAE900", registration="A6-NEW",
            latitude=lat, longitude=lon, on_ground=True,
            baro_altitude=0.0, velocity=0.0,
        )
        source.states = [state]
        asyncio.run(tracker.poll())

    def test_new_airport_is_flagged_unplanned(self) -> None:
        takeoff = AircraftState(
            icao24="uae900", callsign="UAE900", registration="A6-NEW",
            latitude=41.9, longitude=12.4, baro_altitude=3000.0, velocity=200.0,
        )
        source = FakeSource([takeoff])
        tracker = AircraftTracker([source], DummyDB(), self.store, airports=FakeAirports())

        asyncio.run(tracker.poll())
        self._land(tracker, source, 41.80, 12.25)  # أول هبوط: روما
        first = tracker.recent_landings[-1]
        self.assertFalse(first.new_airport)  # لا تاريخ سابق → ليس شذوذاً

        # تقلع ثم تهبط في مطار لم تُرصد فيه من قبل
        source.states = [
            AircraftState(
                icao24="uae900", callsign="UAE900", registration="A6-NEW",
                latitude=25.0, longitude=30.0, baro_altitude=9000.0, velocity=220.0,
            )
        ]
        asyncio.run(tracker.poll())
        self._land(tracker, source, 12.13, 15.04)  # نجامينا
        second = tracker.recent_landings[-1]
        self.assertTrue(second.new_airport)
        self.assertTrue(second.unplanned)
        self.assertIn("مطار", second.unplanned_reason)

    def test_report_shows_unplanned_and_prediction_sections(self) -> None:
        from reporter import build_report

        tracker = AircraftTracker([FakeSource([])], DummyDB(), self.store, airports=FakeAirports())
        report = build_report(tracker, 60)
        self.assertIn("وجهات غير مبرمجة", report)
        self.assertIn("تنبؤ الطابع العسكري", report)


class SahelAnomalyTests(unittest.TestCase):
    """نطاق الساحل الأفريقي، محرّك الشذوذ، ومصفوفة الاستخبارات."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "sahel.db")
        self.tracker = AircraftTracker(
            [FakeSource([])], DummyDB(), self.store, airports=FakeAirports()
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_watchlist_survives_a_fresh_run(self) -> None:
        """قائمة التتبّع العالمي تُحفظ في القاعدة وتُستعاد في تشغيل جديد."""
        from adsb_sources import WatchlistSource

        self.store.add_watch("896123")
        self.assertEqual(self.store.watchlist(), ["896123"])

        watch = WatchlistSource()
        self.assertNotIn("896123", watch.extra_icaos)
        fresh = AircraftTracker(
            [watch], DummyDB(), self.store, airports=FakeAirports()
        )
        self.assertIn("896123", watch.extra_icaos)
        self.assertIn("896123", fresh.store.watchlist())

    def test_scope_names_the_eight_sahel_states_only(self) -> None:
        from sahel import sahel_scope_text

        text = sahel_scope_text()
        for name in ("موريتانيا", "مالي", "بوركينا فاسو", "النيجر", "تشاد",
                     "السودان", "إريتريا", "السنغال"):
            self.assertIn(name, text)
        # دول العبور ليست ساحلاً
        self.assertNotIn("نيجيريا", text)
        self.assertNotIn("ليبيا", text)

    def test_zone_classification(self) -> None:
        from sahel import zone_at

        self.assertEqual(zone_at(14.5, -4.0, "ML"), "sahel")
        self.assertEqual(zone_at(13.5, 2.1, "NE"), "sahel")
        self.assertEqual(zone_at(None, None, "NG"), "transit")
        self.assertEqual(zone_at(15.0, 40.0, None), "maritime")  # البحر الأحمر
        self.assertIsNone(zone_at(48.8, 2.3, "FR"))

    def test_geometry_flags_deviation_and_impossible_speed(self) -> None:
        from sahel import _path_stats, _segments, _teleport_kmh

        straight = [(10.0, 0.0), (11.0, 0.0), (12.0, 0.0), (13.0, 0.0)]
        self.assertLess(_path_stats([(a, b, i) for i, (a, b) in enumerate(straight)])[2], 1.0)

        detour = [(10.0, 0.0), (11.0, 5.0), (12.0, 4.0), (13.0, 0.0)]
        points = [(a, b, i * 1800) for i, (a, b) in enumerate(detour)]
        path_km, straight_km, deviation = _path_stats(points)
        self.assertGreater(deviation, 150)
        self.assertGreater(path_km / straight_km, 1.6)  # التفاف مكتشف

        jump = [(10.0, 0.0, 0), (20.0, 20.0, 600)]  # ~2400 كم في 10 دقائق
        speed, distance = _teleport_kmh(jump)
        self.assertGreater(speed, 1500)
        self.assertGreater(distance, 300)

        # فجوة قصيرة: اهتزاز رصد لا قفزة
        jitter = [(10.0, 0.0, 0), (10.5, 0.5, 10)]
        self.assertEqual(_teleport_kmh(jitter), (0.0, 0.0))

        # رحلتان منفصلتان: لا تُدمجان في هندسة واحدة
        track = [(10.0, 0.0, 0), (11.0, 0.0, 600), (12.0, 0.0, 1200),
                 (13.0, 0.0, 1800), (10.0, 0.0, 9000), (11.0, 0.0, 9600),
                 (12.0, 0.0, 10200), (13.0, 0.0, 10800)]
        self.assertEqual(len(_segments(track)), 2)

    def test_blackout_over_sahel_raises_the_score(self) -> None:
        from datetime import datetime, timezone

        from models import SignalEvent
        from sahel import assess_aircraft

        event = SignalEvent(
            icao24="a1b2c3",
            callsign="UAE777",
            classification="civilian",
            last_seen=datetime.now(timezone.utc),
            last_lat=15.3,
            last_lon=-3.0,
            registration="A6-XXX",
            country="مالي",
            country_code="ML",
        )
        result = assess_aircraft(self.tracker, "a1b2c3", [event], [], None)
        self.assertGreaterEqual(result.score, 0.30)
        self.assertTrue(any("الساحل" in item for item in result.indicators))
        self.assertIn(result.confidence, ("منخفضة", "متوسطة", "مرتفعة"))

    def test_global_broadcast_discounts_the_blackout(self) -> None:
        from datetime import datetime, timezone

        from models import SignalEvent
        from sahel import assess_aircraft

        event = SignalEvent(
            icao24="d4e5f6", callsign="UAE888", classification="civilian",
            last_seen=datetime.now(timezone.utc), last_lat=15.3, last_lon=-3.0,
            country="مالي", country_code="ML", still_broadcasting=True,
            global_country="النيجر",
        )
        result = assess_aircraft(self.tracker, "d4e5f6", [event], [], None)
        self.assertTrue(
            any("تغطية" in item for item in result.indicators),
            "غياب محلي مع بث عالمي يجب أن يُفسَّر كثغرة تغطية",
        )

    def test_matrix_row_has_the_nine_fields_and_visual_links(self) -> None:
        from datetime import datetime, timezone

        from models import SignalEvent
        from sahel import MATRIX_FIELDS, build_matrix

        self.tracker.recent_events = [
            SignalEvent(
                icao24="a1b2c3", callsign="UAE777", classification="civilian",
                last_seen=datetime.now(timezone.utc), last_lat=15.3, last_lon=-3.0,
                registration="A6-XXX", country="مالي", country_code="ML",
            )
        ]
        rows = build_matrix(self.tracker, 24)
        self.assertEqual(len(rows), 1)
        data = rows[0].as_dict()
        for _, label in MATRIX_FIELDS:
            self.assertIn(label, data)
        self.assertIn("apps.sentinel-hub.com", data["Sentinel_EO_Browser"])
        self.assertIn("google.com", data["Google_Earth"])
        self.assertEqual(data["Real_Leasing_Layer"], rows[0].leasing_layer)
        self.assertIn("غير محدد", rows[0].leasing_layer)

    def test_matrix_translates_airport_kind_and_labels_entry(self) -> None:
        from datetime import datetime, timezone

        from models import LandingEvent
        from sahel import build_matrix

        self.store.add_landing(
            LandingEvent(
                icao24="fed789", callsign="UAE557", registration="A6-DB3",
                classification="civilian", uae_related=True,
                airport_ident="FTTJ", airport_name="نجامينا",
                airport_country_code="TD", at=datetime.now(timezone.utc),
                latitude=12.13, longitude=15.04, new_airport=True,
                unplanned=True,
            )
        )
        rows = build_matrix(self.tracker, 24)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIn("مطار إقليمي متوسط", row.landing)  # لا «medium_airport»
        self.assertIn("الساحل", row.entry_point)
        self.assertEqual(row.zone_key, "sahel")
        # داخل نطاق الساحل يبقى الصف حتى لو كانت درجته دون العتبة
        self.assertLess(row.score, 20)

    def test_csv_export_writes_the_schema(self) -> None:
        from datetime import datetime, timezone

        from models import SignalEvent
        from sahel import build_matrix, export_csv

        self.tracker.recent_events = [
            SignalEvent(
                icao24="a1b2c3", callsign="UAE777", classification="civilian",
                last_seen=datetime.now(timezone.utc), last_lat=15.3, last_lon=-3.0,
                country="مالي", country_code="ML",
            )
        ]
        path = export_csv(build_matrix(self.tracker, 24), Path(self.tmp.name) / "m.csv")
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn("Flight_Hex", text)
        self.assertIn("Confidence_Level", text)

    def test_report_has_sahel_scope_and_matrix(self) -> None:
        from reporter import build_report

        report = build_report(self.tracker, 60)
        self.assertIn("نطاق الساحل الأفريقي", report)
        self.assertIn("مصفوفة الاستخبارات", report)
        self.assertIn("موريتانيا", report)

    def test_local_analysis_covers_abnormal_routes(self) -> None:
        from ai_report import build_local_analysis, build_snapshot

        text = build_local_analysis(build_snapshot(self.tracker, 60))
        self.assertIn("## المسارات غير الطبيعية ونطاق الساحل", text)

    def test_window_reads_events_from_database_after_restart(self) -> None:
        """الذاكرة فارغة في كل تشغيل سحابي: النافذة يجب أن تقرأ من القاعدة."""
        from datetime import datetime, timedelta, timezone

        from models import SignalEvent
        from sahel import build_matrix

        self.store.add_event(
            SignalEvent(
                icao24="abc123", callsign="UAE555", classification="civilian",
                last_seen=datetime.now(timezone.utc), last_lat=15.3, last_lon=-3.0,
                registration="A6-DB1", country="مالي", country_code="ML",
            )
        )
        fresh = AircraftTracker(
            [FakeSource([])], DummyDB(), self.store, airports=FakeAirports()
        )
        self.assertEqual(fresh.recent_events, [])  # ذاكرة جديدة فارغة

        events = fresh.events_since(datetime.now(timezone.utc) - timedelta(hours=24))
        self.assertEqual([event.icao24 for event in events], ["abc123"])
        self.assertEqual(events[0].country_code, "ML")

        rows = build_matrix(fresh, 24)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].registration, "A6-DB1")
        self.assertTrue(rows[0].sentinel_url)

    def test_window_reads_landings_from_database_after_restart(self) -> None:
        from datetime import datetime, timedelta, timezone

        from models import LandingEvent

        self.store.add_landing(
            LandingEvent(
                icao24="def456", callsign="UAE556", registration="A6-DB2",
                classification="civilian", uae_related=True,
                airport_ident="FTTJ", airport_name="نجامينا",
                airport_country_code="TD", at=datetime.now(timezone.utc),
                latitude=12.13, longitude=15.04, new_airport=True,
                unplanned=True,
            )
        )
        fresh = AircraftTracker(
            [FakeSource([])], DummyDB(), self.store, airports=FakeAirports()
        )
        self.assertEqual(fresh.recent_landings, [])

        landings = fresh.landings_since(
            datetime.now(timezone.utc) - timedelta(hours=24)
        )
        self.assertEqual([item.icao24 for item in landings], ["def456"])
        self.assertEqual(landings[0].airport_country_code, "TD")
        self.assertTrue(landings[0].unplanned)


class DocumentTests(unittest.TestCase):
    def test_pdf_and_docx_are_created(self) -> None:
        from report_docs import build_documents

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_documents(
                "<b>تقرير تجريبي</b>\n• 3 طائرات إماراتية\n• هبوط في OMDB",
                "<b>الخلاصة</b>\n• نص تجريبي",
                period_minutes=60,
                out_dir=Path(tmp),
            )
            pdf = Path(paths["pdf"])
            docx = Path(paths["docx"])
            self.assertTrue(pdf.exists() and pdf.stat().st_size > 1000)
            self.assertTrue(docx.exists() and docx.stat().st_size > 1000)
            self.assertEqual(pdf.read_bytes()[:4], b"%PDF")

    def test_document_contains_hourly_and_daily_parts(self) -> None:
        """المستند يجمع: دراسة ساعية + تفاصيل + استنتاجات 24 ساعة + تفاصيلها."""
        from docx import Document

        from report_docs import build_documents

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_documents(
                "<b>تفاصيل الساعة</b>\n• سطر",
                "<b>الدراسة</b>\n• نص",
                "<b>استنتاجات اليوم</b>\n• نص",
                "<b>تفاصيل اليوم</b>\n• نص",
                out_dir=Path(tmp),
            )
            text = "\n".join(p.text for p in Document(str(paths["docx"])).paragraphs)
            self.assertIn("الدراسة التحليلية", text)
            self.assertIn("البيانات التفصيلية", text)
            self.assertIn("استنتاجات 24 ساعة", text)
            self.assertIn("تقرير 24 ساعة", text)
            self.assertIn("المحتويات", text)


class OpenSkyBudgetTests(unittest.TestCase):
    """حماية الرصيد المجاني: 400 رصيد/يوم فقط، والدورة كل دقيقتين تُنفده."""

    def test_budget_blocks_after_the_daily_cap(self) -> None:
        from datetime import datetime, timezone

        from adsb_sources import OpenSkySource
        from config import settings

        source = OpenSkySource()
        source._day = datetime.now(timezone.utc).date()
        source._used = settings.opensky_daily_credits
        self.assertFalse(source._budget_allows())

    def test_budget_resets_on_a_new_day(self) -> None:
        from datetime import date

        from adsb_sources import OpenSkySource

        source = OpenSkySource()
        source._day = date(2020, 1, 1)
        source._used = 999
        self.assertTrue(source._budget_allows())
        self.assertEqual(source._used, 0)

    def test_cooldown_blocks_requests_after_429(self) -> None:
        from adsb_sources import OpenSkySource

        source = OpenSkySource()
        source._cool_down(600, "429")
        self.assertFalse(source._budget_allows())

    def test_watchlist_shares_the_same_guard(self) -> None:
        from adsb_sources import WatchlistSource

        self.assertTrue(issubclass(WatchlistSource, __import__("adsb_sources").OpenSkySource))

    def test_watchlist_keeps_newest_aircraft_when_capped(self) -> None:
        from adsb_sources import WatchlistSource
        from config import settings

        source = WatchlistSource()
        for index in range(settings.watchlist_max + 5):
            source.add(f"89{index:04d}")

        self.assertEqual(len(source.icaos), settings.watchlist_max)
        # الأحدث يُتابع، والأقدم يخرج من النافذة
        self.assertIn(f"89{settings.watchlist_max + 4:04d}", source.icaos)
        self.assertNotIn("890000", source.icaos)


class AdsbFiSourceTests(unittest.TestCase):
    """المصدر الاحتياطي: نفس صيغة adsb.lol، مفتاح ``aircraft`` بدل ``ac``."""

    def test_payload_with_aircraft_key_is_parsed(self) -> None:
        from adsb_sources import _parse_adsb_payload

        payload = {
            "now": 1795032288.001,
            "aircraft": [
                {
                    "hex": "896608",
                    "flight": "UAE12   ",
                    "r": "A6-EVN",
                    "t": "A388",
                    "alt_baro": 38000,
                    "gs": 480,
                    "track": 271.5,
                    "baro_rate": -640,
                    "squawk": "4733",
                    "lat": 24.1,
                    "lon": 54.2,
                    "seen": 3,
                },
                {
                    "hex": "~abcdef",
                    "flight": "NOPOS",
                },
                {
                    "hex": "89660a",
                    "flight": "UAE99",
                    "alt_baro": "ground",
                    "gs": 12,
                    "lat": 25.25,
                    "lon": 55.36,
                    "seen": 1,
                },
            ],
            "resultCount": 3,
        }

        states = _parse_adsb_payload(payload, "adsb.fi")

        self.assertEqual([s.icao24 for s in states], ["896608", "89660a"])
        first = states[0]
        self.assertEqual(first.callsign, "UAE12")
        self.assertEqual(first.registration, "A6-EVN")
        self.assertEqual(first.aircraft_type, "A388")
        self.assertEqual(first.source, "adsb.fi")
        self.assertAlmostEqual(first.baro_altitude, 38000 * 0.3048, places=1)
        self.assertAlmostEqual(first.velocity, 480 * 0.514444, places=2)
        self.assertFalse(first.on_ground)
        self.assertIsNotNone(first.last_contact)

        on_ground = states[1]
        self.assertTrue(on_ground.on_ground)
        self.assertEqual(on_ground.baro_altitude, 0.0)

    def test_source_is_registered_in_build_sources(self) -> None:
        from adsb_sources import AdsbFiSource, PointSource, build_sources
        from config import settings

        previous = settings.use_adsb_fi
        settings.use_adsb_fi = True
        try:
            names = [source.name for source in build_sources()]
        finally:
            settings.use_adsb_fi = previous
        self.assertIn("adsb.fi", names)
        self.assertTrue(issubclass(AdsbFiSource, PointSource))

    def test_cooldown_skips_network_after_429(self) -> None:
        import asyncio

        from adsb_sources import AdsbFiSource

        source = AdsbFiSource()
        source._cool_down(600, "429")
        self.assertEqual(asyncio.run(source.fetch()), [])
        self.assertIn("429", source.health.message)


class DailyReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "daily.db")
        self.tracker = AircraftTracker(
            [FakeSource([])], DummyDB(), self.store, airports=FakeAirports()
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_snapshot_is_empty_but_well_formed(self) -> None:
        from daily import build_daily_snapshot

        snap = build_daily_snapshot(self.tracker, 24)
        self.assertEqual(snap["hours"], 24)
        self.assertEqual(snap["totals"]["events"], 0)
        self.assertEqual(snap["totals"]["landings"], 0)
        self.assertIn("from", snap["window"])

    def test_report_has_totals_and_source_quality(self) -> None:
        from daily import build_daily_report

        report = build_daily_report(self.tracker, 24)
        self.assertIn("تقرير 24 ساعة", report)
        self.assertIn("الحصيلة الرقمية", report)
        self.assertIn("جودة المصادر", report)

    def test_local_analysis_ends_with_conclusions(self) -> None:
        from daily import build_daily_local_analysis, build_daily_snapshot

        text = build_daily_local_analysis(build_daily_snapshot(self.tracker, 24))
        self.assertIn("## حصيلة اليوم", text)
        self.assertIn("## الاستنتاجات", text)

    def test_daily_analysis_falls_back_to_local_engine(self) -> None:
        import config
        from daily import build_daily_analysis

        old_enabled, old_mode = config.settings.ai_enabled, config.settings.ai_report_mode
        try:
            config.settings.ai_enabled = False
            config.settings.ai_report_mode = "both"
            html_text, source = asyncio.run(build_daily_analysis(self.tracker, hours=24))
        finally:
            config.settings.ai_enabled, config.settings.ai_report_mode = old_enabled, old_mode
        self.assertEqual(source, "local")
        self.assertIn("استنتاجات 24 ساعة", html_text)
        self.assertIn("الاستنتاجات", html_text)


if __name__ == "__main__":
    unittest.main()
