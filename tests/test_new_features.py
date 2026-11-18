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


if __name__ == "__main__":
    unittest.main()
