"""اختبارات خطّ النماذج، سجل الساحل (Part B)، نطاق ICAO24، وإيقاع 24 ساعة."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from adsb_sources import BaseSource
from classifier import is_uae_related
from models import AircraftState, LandingEvent, SignalEvent
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


class StubTracker:
    """بديل خفيف: السجل يحتاج ``store`` وقائمتي الأحداث والهبوط فقط."""

    def __init__(self, store, events=None, landings=None) -> None:
        self.store = store
        self._events = events or []
        self._landings = landings or []

    def events_since(self, since):
        return list(self._events)

    def landings_since(self, since):
        return list(self._landings)


class RouterTests(unittest.TestCase):
    def test_extract_json_handles_code_fences_and_prose(self) -> None:
        from llm_router import extract_json

        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('نص قبل {"a": 2} نص بعد'), {"a": 2})
        self.assertIsNone(extract_json("لا شيء"))
        self.assertIsNone(extract_json("[1, 2]"))

    def test_validate_rejects_missing_keys(self) -> None:
        from llm_router import validate

        self.assertIsNone(validate({"a": 1}, ("a", "b")))
        self.assertEqual(validate({"a": 1, "b": 2}, ("a", "b")), {"a": 1, "b": 2})
        self.assertIsNone(validate(None, ("a",)))

    def test_role_chain_covers_the_three_providers(self) -> None:
        from llm_router import ROLE_CHAIN

        self.assertEqual(ROLE_CHAIN["filter"], ("groq",))
        self.assertEqual(ROLE_CHAIN["reason"], ("gemini",))
        self.assertEqual(ROLE_CHAIN["audit"], ("openrouter", "groq"))


class DedupKeyTests(unittest.TestCase):
    def test_key_is_stable_and_has_the_three_parts(self) -> None:
        from sahel import dedup_key

        moment = datetime(2026, 11, 18, 12, 30, tzinfo=timezone.utc)
        key = dedup_key("8960D1", moment, "BLK")
        self.assertEqual(key, "8960d1|2026-11-18|1795005000|BLK")
        self.assertEqual(key, dedup_key("8960d1", moment, "BLK"))
        # التاريخ والتوقيت مختلفان ⇒ مفتاح مختلف (لا دمج زائف).
        later = datetime(2026, 11, 18, 13, 30, tzinfo=timezone.utc)
        self.assertNotEqual(key, dedup_key("8960d1", later, "BLK"))

    def test_ledger_reexports_the_same_key(self) -> None:
        import ledger
        import sahel

        self.assertIs(ledger.dedup_key, sahel.dedup_key)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "ledger.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_grid_cell_snaps_to_quarter_degree(self) -> None:
        from ledger import GRID_DEG, grid_cell

        self.assertEqual(GRID_DEG, 0.25)
        self.assertEqual(grid_cell(15.30, -3.02), (15.375, -3.125))
        self.assertIsNone(grid_cell(None, 0))
        self.assertIsNone(grid_cell(999, 0))

    def test_zone_id_is_derived_from_the_grid(self) -> None:
        from ledger import zone_id

        self.assertEqual(zone_id(15.375, -3.125, "sahel"), "SAH-N15.38-W003.12")
        self.assertEqual(zone_id(-4.5, 33.0, "transit"), "TRN-S04.50-E033.00")
        self.assertEqual(zone_id(10.0, 40.0, None), "OUT-N10.00-E040.00")

    def test_sync_and_build_hotspots_group_repeated_blackouts(self) -> None:
        from ledger import build_hotspots, sync

        now = datetime.now(timezone.utc)
        events = [
            SignalEvent(
                icao24="8960d1", callsign="UAE777", classification="civilian",
                last_seen=now, last_lat=15.30, last_lon=-3.02,
                registration="A6-XXX", country="مالي", country_code="ML",
                reason="no_broadcast",
            ),
            # نفس الخلية، طائرة إماراتية أخرى: يجب أن تُجمَّع معها.
            SignalEvent(
                icao24="8960d2", callsign="UAE778", classification="civilian",
                last_seen=now, last_lat=15.31, last_lon=-3.01,
                registration="A6-YYY", country="مالي", country_code="ML",
                reason="no_broadcast",
            ),
            # طائرة غير إماراتية: تُستبعد من السجل.
            SignalEvent(
                icao24="a1b2c3", callsign="AFR123", classification="civilian",
                last_seen=now, last_lat=15.30, last_lon=-3.02,
                registration="F-GABC", country="مالي", country_code="ML",
                reason="no_broadcast",
            ),
            # الخروج من التغطية ليس انقطاعاً: لا يُسجَّل.
            SignalEvent(
                icao24="8960d3", callsign="UAE779", classification="civilian",
                last_seen=now, last_lat=15.30, last_lon=-3.02,
                registration="A6-ZZZ", country="مالي", country_code="ML",
                reason="left_coverage",
            ),
        ]
        tracker = StubTracker(self.store, events=events)
        added = sync(tracker, hours=24)
        self.assertEqual(added["blackouts"], 2)

        # إعادة المزامنة لا تُضيف صفوفاً (idempotent).
        again = sync(tracker, hours=24)
        self.assertEqual(again["blackouts"], 0)

        rows = build_hotspots(tracker, hours=24)
        self.assertEqual(len(rows), 1)
        hotspot = rows[0]
        self.assertEqual(hotspot.frequency, 2)
        self.assertEqual(sorted(hotspot.hex_codes), ["8960D1", "8960D2"])
        self.assertEqual(hotspot.zone_key, "sahel")
        self.assertIn("بانتظار التحقق البصري", hotspot.imint_status)
        self.assertIn("apps.sentinel-hub.com", hotspot.sentinel_url)

    def test_landing_sites_are_recorded_and_merged(self) -> None:
        from ledger import build_hotspots, sync

        now = datetime.now(timezone.utc)
        landings = [
            LandingEvent(
                icao24="8960d1", callsign="UAE777", registration="A6-XXX",
                classification="civilian", uae_related=True,
                airport_ident="FTTJ", airport_name="نجامينا",
                airport_country_code="TD", at=now,
                latitude=12.13, longitude=15.04,
            )
        ]
        tracker = StubTracker(self.store, landings=landings)
        sync(tracker, hours=24)
        rows = build_hotspots(tracker, hours=24)
        self.assertEqual(len(rows), 1)
        self.assertIn("نجامينا", rows[0].airfield)
        self.assertEqual(rows[0].frequency, 1)

    def test_audit_roundtrip(self) -> None:
        self.store.record_audit("8960d1", "2026-11-18", "nvidia/nemotron", "upheld", "ok")
        audit = self.store.audit_for("8960d1")
        self.assertIsNotNone(audit)
        self.assertEqual(audit["model"], "nvidia/nemotron")
        self.assertEqual(audit["verdict"], "upheld")

    def test_llm_call_budget_counts_today(self) -> None:
        self.store.record_llm_call("groq", "filter", True)
        self.store.record_llm_call("groq", "filter", False)
        self.assertEqual(self.store.llm_calls_today("groq"), 2)
        self.assertEqual(self.store.llm_calls_today("gemini"), 0)


class UaeIcaoRangeTests(unittest.TestCase):
    def test_national_range_is_treated_as_uae(self) -> None:
        inside = AircraftState(icao24="8960d1", callsign=None, registration=None)
        self.assertTrue(is_uae_related(inside))

    def test_outside_range_and_no_markers_is_not_uae(self) -> None:
        outside = AircraftState(icao24="a1b2c3", callsign="AFR123", registration="F-GABC")
        self.assertFalse(is_uae_related(outside))

    def test_non_hex_icao_does_not_crash(self) -> None:
        weird = AircraftState(icao24="uae900", callsign="XYZ", registration=None)
        self.assertFalse(is_uae_related(weird))


class PipelineRecordsTests(unittest.TestCase):
    def test_records_are_deduplicated_and_uae_only(self) -> None:
        from pipeline import dedupe_records, records_from_snapshot

        snapshot = {
            "sahel": {
                "matrix": [
                    {
                        "Flight_Hex": "8960D1", "Registration": "A6-XXX",
                        "MSN": "غير متوفر", "Declared_Operator": "الإمارات",
                        "Origin": "OMDB", "Entry_Point_Sahel": "مالي",
                        "Blackout_LatLong": "15.3, -3.0", "Landing_Airfield": "—",
                        "Anomaly_Score": 40, "Confidence_Level": "متوسطة",
                        "Anomaly_Indicators": "انقطاع؛ هبوط", "Zone": "الساحل",
                        "Dedup_Key": "8960d1|2026-11-18|100|MAT",
                    },
                    {
                        # صف مكرّر بالمفتاح نفسه وبدرجة أدنى: يُسقط.
                        "Flight_Hex": "8960D1", "Registration": "A6-XXX",
                        "Anomaly_Score": 10,
                        "Dedup_Key": "8960d1|2026-11-18|100|MAT",
                    },
                    {
                        # غير إماراتية: تُستبعد.
                        "Flight_Hex": "A1B2C3", "Registration": "F-GABC",
                        "Anomaly_Score": 90,
                        "Dedup_Key": "a1b2c3|2026-11-18|100|MAT",
                    },
                ]
            },
            "signal_loss_uae": [],
        }
        records = dedupe_records(records_from_snapshot(snapshot))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["hex"], "8960d1")
        self.assertEqual(records[0]["score"], 40)

    def test_msn_is_declared_unavailable_never_guessed(self) -> None:
        from pipeline import MSN_UNAVAILABLE, records_from_snapshot

        snapshot = {
            "signal_loss_uae": [
                {
                    "callsign": "UAE777", "registration": "A6-XXX",
                    "country": "مالي", "reason": "غياب بث", "minutes_ago": 5,
                }
            ]
        }
        records = records_from_snapshot(snapshot)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["msn"], MSN_UNAVAILABLE)


class DailyGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        import main

        self.main = main
        self.old_marker = main.DAILY_MARKER
        main.DAILY_MARKER = Path(self.tmp.name) / "last-daily.txt"

    def tearDown(self) -> None:
        self.main.DAILY_MARKER = self.old_marker
        self.tmp.cleanup()

    def test_no_marker_means_include_daily(self) -> None:
        self.assertTrue(self.main._should_include_daily())

    def test_recent_marker_suppresses_daily(self) -> None:
        self.main._mark_daily_sent()
        self.assertFalse(self.main._should_include_daily())

    def test_force_overrides_the_daily_guard(self) -> None:
        self.main._mark_daily_sent()
        self.assertTrue(self.main._should_include_daily(force=True))

    def test_old_marker_reenables_daily(self) -> None:
        old = time.time() - (self.main.settings.daily_guard_hours + 1) * 3600
        self.main.DAILY_MARKER.write_text(str(old), encoding="utf-8")
        self.assertTrue(self.main._should_include_daily())


class UaeOnlyReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "report.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _tracker(self, states: list[AircraftState]) -> AircraftTracker:
        return AircraftTracker([FakeSource(states)], DummyDB(), self.store)

    def test_report_details_only_uae_aircraft(self) -> None:
        from reporter import build_report

        states = [
            AircraftState(
                icao24="8960d1", callsign="UAE123", registration="A6-ABC",
                aircraft_type="B738", latitude=25.0, longitude=55.0,
                baro_altitude=10000.0, velocity=230.0,
            ),
            AircraftState(
                icao24="a1b2c3", callsign="AFR999", registration="F-GABC",
                aircraft_type="A320", latitude=25.1, longitude=55.1,
                baro_altitude=10000.0, velocity=230.0,
            ),
        ]
        tracker = self._tracker(states)
        asyncio.run(tracker.poll())
        text = build_report(tracker, 60)
        self.assertIn("UAE123", text)
        self.assertNotIn("AFR999", text)
        self.assertIn("سياق عام", text)

    def test_matrix_includes_new_part_a_fields(self) -> None:
        from sahel import MATRIX_FIELDS, MSN_UNAVAILABLE, build_matrix

        tracker = self._tracker([])
        tracker.recent_events = [
            SignalEvent(
                icao24="8960d1", callsign="UAE777", classification="civilian",
                last_seen=datetime.now(timezone.utc), last_lat=15.3, last_lon=-3.0,
                registration="A6-XXX", country="مالي", country_code="ML",
            )
        ]
        rows = build_matrix(tracker, 24)
        self.assertEqual(len(rows), 1)
        data = rows[0].as_dict()
        for _, label in MATRIX_FIELDS:
            self.assertIn(label, data)
        self.assertEqual(data["MSN"], MSN_UNAVAILABLE)
        self.assertIn("غير محدد", data["Origin"])
        self.assertTrue(data["Dedup_Key"].startswith("8960d1|"))
        self.assertIn(data["Audited_By_Model"], ("غير مُدقَّق",))


class PipelineEndToEndTests(unittest.TestCase):
    """خطّ كامل بمزوّدين مزيّفين: يتحقّق من التدقيق الإلزامي والتراجع."""

    def test_audit_failure_returns_none_instead_of_unverified_text(self) -> None:
        from llm_router import LLMReply, LLMRouter
        from pipeline import run_analysis

        class FakeRouter(LLMRouter):
            def __init__(self) -> None:
                super().__init__(store=None)

            @property
            def available(self) -> bool:  # type: ignore[override]
                return True

            async def call(self, role, system, prompt, required=(), max_tokens=None,
                           temperature=0.0):  # type: ignore[override]
                if role == "filter":
                    return LLMReply({"deduped_keys": []}, "groq", "fake-groq")
                if role == "reason":
                    return LLMReply(
                        {"sections": [{"heading": "قسم", "points": ["نقطة"]}]},
                        "gemini", "fake-gemini",
                    )
                return None  # فشل التدقيق

        snapshot = {
            "sahel": {
                "matrix": [
                    {
                        "Flight_Hex": "8960D1", "Registration": "A6-XXX",
                        "Anomaly_Score": 40,
                        "Dedup_Key": "8960d1|2026-11-18|100|MAT",
                    }
                ]
            }
        }
        result = asyncio.run(run_analysis(snapshot, tracker=None, router=FakeRouter()))
        self.assertIsNone(result)

    def test_audited_result_is_rendered_and_returned(self) -> None:
        from llm_router import LLMReply, LLMRouter
        from pipeline import run_analysis

        class FakeRouter(LLMRouter):
            def __init__(self) -> None:
                super().__init__(store=None)

            @property
            def available(self) -> bool:  # type: ignore[override]
                return True

            async def call(self, role, system, prompt, required=(), max_tokens=None,
                           temperature=0.0):  # type: ignore[override]
                if role == "filter":
                    return LLMReply({"deduped_keys": ["8960d1|2026-11-18|100|MAT"]},
                                    "groq", "fake-groq")
                if role == "reason":
                    return LLMReply(
                        {"sections": [{"heading": "قسم", "points": ["نقطة"]}]},
                        "gemini", "fake-gemini",
                    )
                return LLMReply(
                    {
                        "sections": [{"heading": "قسم مدقَّق", "points": ["نقطة مدقَّقة"]}],
                        "audit": [{"hex": "8960d1", "verdict": "upheld", "note": "مسند"}],
                        "conclusions": ["استنتاج"],
                    },
                    "openrouter", "fake-auditor",
                )

        snapshot = {
            "sahel": {
                "matrix": [
                    {
                        "Flight_Hex": "8960D1", "Registration": "A6-XXX",
                        "Anomaly_Score": 40,
                        "Dedup_Key": "8960d1|2026-11-18|100|MAT",
                    }
                ]
            }
        }
        result = asyncio.run(run_analysis(snapshot, tracker=None, router=FakeRouter()))
        self.assertIsNotNone(result)
        self.assertIn("قسم مدقَّق", result.text)
        self.assertIn("التدقيق المتقاطع", result.text)
        self.assertEqual(result.audit["8960d1"], "fake-auditor")
        self.assertEqual(result.verdicts["8960d1"], "upheld")


if __name__ == "__main__":
    unittest.main()
