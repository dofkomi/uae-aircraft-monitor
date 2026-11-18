"""قاعدة بيانات المطارات المجانية (OurAirports — بيانات عامة).

يُحمّل الملف مرة واحدة ويُخزّن محلياً بنسخة مُصفّاة خفيفة، ثم يُستخدم
لتحديد أقرب مطار لنقطة الهبوط/الإقلاع دون أي مفتاح API.
"""
from __future__ import annotations

import csv
import io
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
WANTED_TYPES = {"large_airport", "medium_airport", "small_airport"}
KEEP_COLUMNS = ("ident", "iata_code", "name", "latitude_deg", "longitude_deg", "iso_country", "type")

COUNTRY_NAMES = {
    "AE": "الإمارات", "SA": "السعودية", "OM": "عُمان", "QA": "قطر", "BH": "البحرين",
    "KW": "الكويت", "IQ": "العراق", "IR": "إيران", "TR": "تركيا", "EG": "مصر",
    "SD": "السودان", "TD": "تشاد", "LY": "ليبيا", "IT": "إيطاليا", "FR": "فرنسا",
    "DE": "ألمانيا", "GB": "بريطانيا", "DJ": "جيبوتي", "ET": "إثيوبيا", "SO": "الصومال",
    "YE": "اليمن", "JO": "الأردن", "SY": "سوريا", "LB": "لبنان", "IL": "إسرائيل",
    "AF": "أفغانستان", "PK": "باكستان", "IN": "الهند", "CN": "الصين", "RU": "روسيا",
    "US": "أمريكا", "CA": "كندا", "BR": "البرازيل", "NG": "نيجيريا", "CM": "الكاميرون",
    "NE": "النيجر", "ML": "مالي", "BF": "بوركينا فاسو", "CI": "ساحل العاج", "GH": "غانا",
    "KE": "كينيا", "TZ": "تنزانيا", "UG": "أوغندا", "CD": "الكونغو الديمقراطية",
    "AO": "أنغولا", "ZA": "جنوب أفريقيا", "ES": "إسبانيا", "PT": "البرتغال",
    "NL": "هولندا", "BE": "بلجيكا", "CH": "سويسرا", "AT": "النمسا", "GR": "اليونان",
    "PL": "بولندا", "SE": "السويد", "NO": "النرويج", "FI": "فنلندا", "DK": "الدنمارك",
    "IE": "أيرلندا", "CZ": "التشيك", "HU": "المجر", "RO": "رومانيا", "UA": "أوكرانيا",
    "GE": "جورجيا", "AM": "أرمينيا", "AZ": "أذربيجان", "KZ": "كازاخستان",
    "UZ": "أوزبكستان", "TM": "تركمانستان", "JP": "اليابان", "KR": "كوريا الجنوبية",
    "TH": "تايلاند", "MY": "ماليزيا", "SG": "سنغافورة", "ID": "إندونيسيا",
    "PH": "الفلبين", "VN": "فيتنام", "AU": "أستراليا", "NZ": "نيوزيلندا",
    "MA": "المغرب", "TN": "تونس", "DZ": "الجزائر", "SN": "السنغال", "MR": "موريتانيا",
    "ER": "إريتريا", "MG": "مدغشقر", "RW": "رواندا", "BI": "بوروندي", "SS": "جنوب السودان",
}


def country_name(code: str | None) -> str:
    if not code:
        return "غير محدد"
    return COUNTRY_NAMES.get(code.upper(), code.upper())


@dataclass(frozen=True)
class Airport:
    ident: str
    iata: str
    name: str
    lat: float
    lon: float
    country_code: str
    kind: str


class AirportDB:
    def __init__(self, cache_path: Path, url: str = OURAIRPORTS_URL) -> None:
        self.cache_path = Path(cache_path)
        self.url = url
        self.airports: list[Airport] = []
        self._load()

    def _load(self) -> None:
        if not self.cache_path.exists():
            self._write_filtered(self._download())
        try:
            with self.cache_path.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    try:
                        lat = float(row["latitude_deg"])
                        lon = float(row["longitude_deg"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    self.airports.append(
                        Airport(
                            ident=row.get("ident", ""),
                            iata=row.get("iata_code", ""),
                            name=row.get("name", ""),
                            lat=lat,
                            lon=lon,
                            country_code=row.get("iso_country", ""),
                            kind=row.get("type", ""),
                        )
                    )
        except (OSError, csv.Error) as exc:
            log.warning("تعذّر تحميل المطارات: %s", exc)
        log.info("تم تحميل %d مطاراً من %s", len(self.airports), self.cache_path.name)

    def _download(self) -> str:
        """تنزيل لمرة واحدة عبر المكتبة القياسية (يعمل داخل أو خارج حدث async)."""
        try:
            request = Request(self.url, headers={"User-Agent": "uae-aircraft-monitor/1.0"})
            with urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - نتجاوز الفشل ونكمل بدون مطارات
            log.error("تعذّر تنزيل بيانات المطارات: %s", exc)
            return ""

    def _write_filtered(self, raw: str) -> None:
        if not raw:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        reader = csv.DictReader(io.StringIO(raw))
        with self.cache_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(KEEP_COLUMNS))
            writer.writeheader()
            for row in reader:
                if row.get("type") not in WANTED_TYPES:
                    continue
                writer.writerow({key: row.get(key, "") for key in KEEP_COLUMNS})

    def nearest(self, lat: float, lon: float, max_km: float = 25.0) -> Airport | None:
        best: Airport | None = None
        best_km = max_km
        for airport in self.airports:
            distance = haversine_km(lat, lon, airport.lat, airport.lon)
            if distance < best_km:
                best_km, best = distance, airport
        return best


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))
