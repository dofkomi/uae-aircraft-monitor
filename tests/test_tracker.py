"""اختبارات وحدة بسيطة (تستخدم unittest من المكتبة القياسية)."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from adsb_sources import BaseSource
from airports import Airport
from classifier import CIVILIAN, MILITARY, classify, is_uae_related
from models import AircraftState
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
        # طائرة إماراتية: روما ← نجامينا (كلتاهما خارج الإمارات)
        callsign = "UAE777"
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
        source = FakeSource([at_rome])
        tracker = AircraftTracker([source], self.db, self.store, airports=FakeAirports())

        asyncio.run(tracker.poll())
        source.states = [en_route]
        asyncio.run(tracker.poll())
        source.states = [at_ndjamena]
        asyncio.run(tracker.poll())

        landings = tracker.recent_landings
        self.assertEqual(len(landings), 2)
        self.assertEqual(landings[0].airport_ident, "LIRF")
        self.assertEqual(landings[1].airport_ident, "FTTJ")
        self.assertEqual(landings[1].from_airport, "LIRF")
        self.assertTrue(landings[1].divergence)
        self.assertTrue(landings[1].uae_related)


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


if __name__ == "__main__":
    unittest.main()
