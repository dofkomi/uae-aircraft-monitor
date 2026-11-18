"""توليد نسختَي PDF و DOCX من تقرير المراقبة (دراسة + تفاصيل + 24 ساعة).

تليجرام يعرض الدراسة التحليلية فقط؛ هنا تُجمع كل البيانات التفصيلية في
مستند واحد قابل للاستغلال (بحث، نسخ، طباعة، أرشفة).
يعمل بلا إنترنت: الخط العربي (Amiri) مضمّن في ``data/fonts``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

ROOT = Path(__file__).resolve().parent
FONT_DIR = ROOT / "data" / "fonts"
FONT_REGULAR = FONT_DIR / "Amiri-Regular.ttf"
FONT_BOLD = FONT_DIR / "Amiri-Bold.ttf"

log = logging.getLogger("report_docs")

_TAG_RE = re.compile(r"<[^>]+>")
_BOLD_SPLIT = re.compile(r"(<b>.*?</b>)", re.S)
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")

DEFAULT_TITLE = "تقرير مراقبة الطائرات — الإمارات"


# ---------------------------------------------------------------------------
# تحويل HTML تليجرام إلى مقاطع نصية
# ---------------------------------------------------------------------------
def _segments(line: str) -> list[tuple[str, bool]]:
    """يقسّم سطر HTML إلى مقاطع (نص، عريض) مع فكّ الكيانات."""
    parts: list[tuple[str, bool]] = []
    for chunk in _BOLD_SPLIT.split(line):
        if not chunk:
            continue
        bold = chunk.startswith("<b>") and chunk.endswith("</b>")
        text = html.unescape(_TAG_RE.sub("", chunk))
        if text:
            parts.append((text, bold))
    return parts


def _plain(line: str) -> str:
    return "".join(text for text, _ in _segments(line)) or html.unescape(
        _TAG_RE.sub("", line)
    )


def _is_heading(line: str) -> bool:
    """سطر عنوان: قصير ويبدأ برمز تعبيري أو ينتهي بنقطتين."""
    plain = _plain(line).strip()
    if not plain or len(plain) > 70:
        return False
    return plain.endswith(":") or bool(re.match(r"^[^\w\u0600-\u06FF]{1,3}\s*\S", plain))


def _shape(text: str) -> str:
    """تشكيل عربي + ترتيب ثنائي الاتجاه لـ reportlab."""
    if not _ARABIC_RE.search(text):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except Exception:  # noqa: BLE001 - نُبقي النص كما هو بدل إفشال التقرير
        return text


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M")


def build_sections(
    detail_html: str,
    analysis_html: str | None = None,
    daily_analysis_html: str | None = None,
    daily_detail_html: str | None = None,
    period_minutes: int = 60,
    hours: int = 24,
) -> list[tuple[str, str]]:
    """يبني أقسام المستند بالترتيب: دراسة، تفاصيل، استنتاجات 24 ساعة، تفاصيل 24 ساعة."""
    sections: list[tuple[str, str]] = []
    if analysis_html:
        sections.append((f"الدراسة التحليلية — آخر {period_minutes} دقيقة", analysis_html))
    if detail_html:
        sections.append(
            (f"البيانات التفصيلية — آخر {period_minutes} دقيقة", detail_html)
        )
    if daily_analysis_html:
        sections.append((f"استنتاجات {hours} ساعة", daily_analysis_html))
    if daily_detail_html:
        sections.append((f"تقرير {hours} ساعة — البيانات التفصيلية", daily_detail_html))
    return sections


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def _docx_set_rtl(paragraph) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    p_pr.append(OxmlElement("w:bidi"))
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT


def _docx_run_rtl(run) -> None:
    r_pr = run._element.get_or_add_rPr()
    r_pr.append(OxmlElement("w:rtl"))


def _docx_page_numbers(section) -> None:
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run()
    for tag, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if tag:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), tag)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)


def build_docx(
    sections: list[tuple[str, str]],
    title: str,
    subtitle: str,
    now: datetime,
    out_dir: Path,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"uae-aircraft-report-{_stamp(now)}.docx"

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Amiri"
    style.font.size = Pt(11)
    style.element.rPr.rFonts.set(qn("w:cs"), "Amiri")

    _docx_page_numbers(doc.sections[0])

    head = doc.add_paragraph()
    _docx_set_rtl(head)
    run = head.add_run(title)
    run.bold = True
    run.font.size = Pt(20)
    _docx_run_rtl(run)

    sub = doc.add_paragraph()
    _docx_set_rtl(sub)
    r = sub.add_run(subtitle)
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    _docx_run_rtl(r)

    # فهرس المحتويات
    if len(sections) > 1:
        index_head = doc.add_paragraph()
        _docx_set_rtl(index_head)
        ir = index_head.add_run("المحتويات")
        ir.bold = True
        ir.font.size = Pt(13)
        _docx_run_rtl(ir)
        for number, (heading, _) in enumerate(sections, start=1):
            item = doc.add_paragraph()
            _docx_set_rtl(item)
            ir = item.add_run(f"{number}. {heading}")
            ir.font.size = Pt(10)
            _docx_run_rtl(ir)

    def add_block(block_html: str) -> None:
        for raw in block_html.split("\n"):
            line = raw.rstrip()
            if not line.strip():
                doc.add_paragraph()
                continue
            heading = _is_heading(line)
            paragraph = doc.add_paragraph()
            _docx_set_rtl(paragraph)
            for text, bold in _segments(line):
                run = paragraph.add_run(text)
                run.bold = bold or heading
                if heading:
                    run.font.size = Pt(13)
                _docx_run_rtl(run)

    for number, (heading, block) in enumerate(sections, start=1):
        if number > 1:
            doc.add_page_break()
        part = doc.add_paragraph()
        _docx_set_rtl(part)
        pr = part.add_run(f"{number}. {heading}")
        pr.bold = True
        pr.font.size = Pt(16)
        pr.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
        _docx_run_rtl(pr)
        add_block(block)

    doc.save(path)
    return path


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def build_pdf(
    sections: list[tuple[str, str]],
    title: str,
    subtitle: str,
    now: datetime,
    out_dir: Path,
) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"uae-aircraft-report-{_stamp(now)}.pdf"

    if not FONT_REGULAR.exists():
        raise FileNotFoundError(
            "خط عربي مفقود: data/fonts/Amiri-Regular.ttf — لا يمكن توليد PDF."
        )
    if "Amiri" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Amiri", str(FONT_REGULAR)))
    if FONT_BOLD.exists() and "Amiri-Bold" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Amiri-Bold", str(FONT_BOLD)))
    bold_name = "Amiri-Bold" if FONT_BOLD.exists() else "Amiri"

    body = ParagraphStyle(
        "body", fontName="Amiri", fontSize=10.5, leading=16, alignment=2, spaceAfter=2
    )
    heading = ParagraphStyle(
        "heading", parent=body, fontName=bold_name, fontSize=13, leading=19
    )
    heading.spaceBefore = 8
    heading.spaceAfter = 4
    part_style = ParagraphStyle(
        "part", parent=body, fontName=bold_name, fontSize=16, leading=22,
        textColor="#1F4E79", spaceAfter=6,
    )
    title_style = ParagraphStyle(
        "title", parent=body, fontName=bold_name, fontSize=20, leading=26, alignment=1
    )
    sub_style = ParagraphStyle(
        "sub", parent=body, fontSize=9, leading=14, alignment=1, textColor="#555555"
    )

    story: list = [Paragraph(_shape(title), title_style), Spacer(1, 4)]
    story.append(Paragraph(_shape(subtitle), sub_style))
    story.append(Spacer(1, 10))

    if len(sections) > 1:
        story.append(Paragraph(_shape("المحتويات"), heading))
        for number, (name, _) in enumerate(sections, start=1):
            story.append(Paragraph(_shape(f"{number}. {name}"), body))
        story.append(Spacer(1, 8))

    def add_block(block_html: str) -> None:
        for raw in block_html.split("\n"):
            line = raw.rstrip()
            if not line.strip():
                story.append(Spacer(1, 5))
                continue
            plain = _plain(line).strip()
            if not plain:
                continue
            style = heading if _is_heading(line) else body
            safe = plain.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(_shape(safe), style))

    for number, (name, block) in enumerate(sections, start=1):
        if number > 1:
            story.append(PageBreak())
        story.append(Paragraph(_shape(f"{number}. {name}"), part_style))
        add_block(block)

    def _footer(canvas, doc_) -> None:
        canvas.saveState()
        canvas.setFont("Amiri", 8)
        canvas.drawCentredString(A4[0] / 2, 12 * mm, str(canvas.getPageNumber()))
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=title,
    )
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return path


# ---------------------------------------------------------------------------
def build_documents(
    detail_html: str,
    analysis_html: str | None = None,
    daily_analysis_html: str | None = None,
    daily_detail_html: str | None = None,
    title: str = DEFAULT_TITLE,
    period_minutes: int = 60,
    hours: int = 24,
    now: datetime | None = None,
    out_dir: Path | None = None,
) -> dict[str, Path]:
    """يولّد الملفين ويعيد مساريهما. لا يرفع استثناءً إن فشل PDF فقط."""
    now = now or datetime.now(timezone.utc)
    out_dir = out_dir or ROOT / "data" / "reports"
    sections = build_sections(
        detail_html,
        analysis_html,
        daily_analysis_html,
        daily_detail_html,
        period_minutes=period_minutes,
        hours=hours,
    )
    subtitle = (
        f"{now.strftime('%Y-%m-%d %H:%M')} UTC · آخر {period_minutes} دقيقة "
        f"+ تقرير {hours} ساعة"
    )

    paths: dict[str, Path] = {}
    paths["docx"] = build_docx(sections, title, subtitle, now, out_dir)
    try:
        paths["pdf"] = build_pdf(sections, title, subtitle, now, out_dir)
    except Exception as exc:  # noqa: BLE001 - DOCX وحده أفضل من لا شيء
        paths["pdf_error"] = str(exc)  # type: ignore[assignment]
    _prune(out_dir)
    return paths


def _prune(out_dir: Path, keep: int = 6) -> None:
    """يحذف أقدم الملفات حتى لا تتراكم نسخ ضخمة في السحابة."""
    for pattern in ("*.pdf", "*.docx"):
        files = sorted(out_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
        for old in files[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
async def send_documents(
    tg_bot,
    chat_id: int,
    tracker,
    analysis_html: str | None = None,
    period_minutes: int = 60,
    hours: int = 24,
    include_daily: bool = True,
) -> None:
    """يبني PDF و DOCX (دراسة + تفاصيل + 24 ساعة) ويرسلهما كمستندين.

    ``tg_bot`` أي كائن يوفّر ``send_document`` (مثل ``telegram.Bot``).
    لا يرفع استثناءً: فشل المستندات لا يُفقد الدراسة النصية.
    """
    from daily import build_daily_analysis, build_daily_report
    from reporter import build_report

    try:
        detail = build_report(tracker, period_minutes)
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذّر بناء التقرير التفصيلي: %s", exc)
        return

    daily_detail = ""
    daily_analysis = ""
    if include_daily:
        try:
            daily_detail = build_daily_report(tracker, hours)
            daily_analysis, _ = await build_daily_analysis(tracker, hours=hours)
        except Exception as exc:  # noqa: BLE001 - التقرير الساعي يكفي
            log.warning("تعذّر بناء تقرير 24 ساعة: %s", exc)

    try:
        paths = build_documents(
            detail,
            analysis_html,
            daily_analysis or None,
            daily_detail or None,
            period_minutes=period_minutes,
            hours=hours,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذّر توليد ملفات التقرير: %s", exc)
        return

    captions = {
        "pdf": "📄 نسخة PDF — دراسة + تفاصيل + تقرير 24 ساعة (للطباعة والأرشفة)",
        "docx": "📝 نسخة DOCX — قابلة للتعديل والاستخراج",
    }
    for key, caption in captions.items():
        path = paths.get(key)
        if not path:
            continue
        path = Path(path)
        if not path.exists():
            continue
        for attempt in range(1, 4):
            try:
                with path.open("rb") as handle:
                    await tg_bot.send_document(
                        chat_id=chat_id,
                        document=handle,
                        filename=path.name,
                        caption=caption,
                    )
                break
            except Exception as exc:  # noqa: BLE001
                # رفع الملفات يتعثّر أحياناً بشبكة متقطّعة؛ نعيد المحاولة
                # قبل إعلان الفشل (الملف لم يُسلَّم فلا خطر تكرار).
                log.warning(
                    "تعذّر إرسال %s إلى %s (محاولة %d/3): %s",
                    key, chat_id, attempt, exc,
                )
                if attempt == 3:
                    break
                await asyncio.sleep(4 * attempt)
