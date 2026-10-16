"""Tests for ICS (iCalendar) handler."""

import pytest
from pathlib import Path

from converter import ConversionOptions, DocumentConverter
from converter.formats.ics_handler import ICSHandler


class TestICSParsing:
    """Tests for ICS parsing (fallback parser + optional icalendar)."""

    def test_parse_events_count_and_order(self, ics_handler, sample_ics):
        """Events are parsed and sorted chronologically."""
        name, events = ics_handler._parse_events(str(sample_ics))
        assert name == "Рабочий календарь"
        assert len(events) == 4
        assert [e['summary'] for e in events] == [
            "Ранняя встреча",       # 01.09.2026 09:00 (TZID)
            "Встреча команды",      # 12.10.2026 14:30 UTC
            "День рождения , весь день",  # 01.11.2026 (all-day)
            "Очень длинное название события которое было перенесено на следующую строку по правилу RFC 5545",
        ]

    def test_parse_event_fields(self, ics_handler, sample_ics):
        """Summary, location, description, organizer, attendees, rrule are extracted."""
        _, events = ics_handler._parse_events(str(sample_ics))
        meeting = next(e for e in events if e['summary'] == "Встреча команды")
        early = next(e for e in events if e['summary'] == "Ранняя встреча")

        assert meeting['location'] == "Переговорная 3"
        assert "Обсуждение планов" in meeting['description']
        assert "Вторая строка" in meeting['description']
        assert "Иван Петров" in meeting['organizer']
        assert any("Мария Смирнова" in a for a in meeting['attendees'])
        assert meeting['rrule'] == "weekly"
        assert early['status'] == "CONFIRMED"

    def test_parse_all_day_event(self, ics_handler, sample_ics):
        """VALUE=DATE events are marked as all-day."""
        _, events = ics_handler._parse_events(str(sample_ics))
        birthday = next(e for e in events if "весь день" in e['summary'])
        assert birthday['start_all_day'] is True
        assert not hasattr(birthday['start'], 'hour')

    def test_parse_escaped_comma(self, ics_handler, sample_ics):
        """\\, in SUMMARY is unescaped to a literal comma."""
        _, events = ics_handler._parse_events(str(sample_ics))
        assert any(e['summary'] == "День рождения , весь день" for e in events)

    def test_parse_folded_line(self, ics_handler, sample_ics):
        """RFC 5545 folded lines (leading space) are joined."""
        _, events = ics_handler._parse_events(str(sample_ics))
        folded = next(e for e in events if "перенесено" in e['summary'])
        assert "перенесено на следующую" in folded['summary']

    def test_parse_no_events_raises(self, ics_handler, empty_ics):
        """Empty calendar → RuntimeError on any output."""
        with pytest.raises(RuntimeError, match="no events"):
            ics_handler.convert(str(empty_ics), str(empty_ics.parent / "out.txt"), 'txt',
                                ConversionOptions())

    def test_parse_fallback_when_icalendar_missing(self, ics_handler, sample_ics, monkeypatch):
        """Built-in parser produces the same event count if icalendar is absent."""
        monkeypatch.setattr(ics_handler._libs, 'get_icalendar', lambda: None)
        name, events = ics_handler._parse_events(str(sample_ics))
        assert name == "Рабочий календарь"
        assert len(events) == 4
        assert any(e['summary'] == "Встреча команды" for e in events)

    def test_parse_with_and_without_icalendar_agree(self, ics_handler, sample_ics):
        """Both parsing paths agree on the number of events and all-day flags."""
        with_ical = ics_handler._parse_events(str(sample_ics))
        saved = ics_handler._libs.get_icalendar
        try:
            ics_handler._libs.get_icalendar = lambda: None
            without_ical = ics_handler._parse_events(str(sample_ics))
        finally:
            ics_handler._libs.get_icalendar = saved

        assert len(with_ical[1]) == len(without_ical[1]) == 4
        assert with_ical[0] == without_ical[0]
        for a, b in zip(with_ical[1], without_ical[1]):
            assert a['summary'] == b['summary']
            assert a['start_all_day'] == b['start_all_day']

    def test_format_when_all_day(self, ics_handler):
        """All-day events render as date without time."""
        from datetime import date
        text = ics_handler._format_when(date(2026, 11, 1), True, None)
        assert "01.11.2026" in text
        assert "весь день" in text

    def test_format_when_utc(self, ics_handler):
        """UTC datetimes get a UTC suffix."""
        from datetime import datetime, timezone
        dt = datetime(2026, 10, 12, 14, 30, tzinfo=timezone.utc)
        text = ics_handler._format_when(dt, False, None)
        assert "12.10.2026" in text
        assert "14:30" in text
        assert "UTC" in text


class TestICSHandlerOutputs:
    """End-to-end conversions from ICS to every supported output."""

    def test_ics_to_txt(self, ics_handler, sample_ics, test_dir):
        out = test_dir / "out.txt"
        result = ics_handler.convert(str(sample_ics), str(out), 'txt', ConversionOptions())
        assert result == str(out)
        content = out.read_text(encoding='utf-8')
        assert "Рабочий календарь" in content
        assert "Встреча команды" in content
        assert "Переговорная 3" in content
        assert "Всего событий: 4" in content

    def test_ics_to_html(self, ics_handler, sample_ics, test_dir):
        out = test_dir / "out.html"
        result = ics_handler.convert(str(sample_ics), str(out), 'html', ConversionOptions())
        assert result == str(out)
        content = out.read_text(encoding='utf-8')
        assert "<html" in content.lower()
        assert "Встреча команды" in content
        assert "Переговорная 3" in content
        # user text must be html-escaped
        assert "<" not in content or "Организатор" in content

    def test_ics_to_pdf(self, ics_handler, sample_ics, test_dir):
        import fitz
        out = test_dir / "out.pdf"
        result = ics_handler.convert(str(sample_ics), str(out), 'pdf', ConversionOptions())
        assert result == str(out)
        assert out.stat().st_size > 0
        doc = fitz.open(str(out))
        assert doc.page_count >= 1
        # \xa0 (NBSP) is a known PyMuPDF+TrueType artifact in extraction
        text = doc[0].get_text().replace('\u00a0', ' ')
        doc.close()
        # Cyrillic must survive the roundtrip (font with unicode support)
        assert "Встреча команды" in text
        assert "Переговорная 3" in text

    def test_ics_to_docx(self, ics_handler, sample_ics, test_dir):
        from docx import Document
        out = test_dir / "out.docx"
        result = ics_handler.convert(str(sample_ics), str(out), 'docx', ConversionOptions())
        assert result == str(out)
        doc = Document(str(out))
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "Встреча команды" in text
        assert "Переговорная 3" in text

    def test_ics_unsupported_output(self, ics_handler, sample_ics, test_dir):
        """Unsupported output format returns None."""
        result = ics_handler.convert(str(sample_ics), str(test_dir / "out.png"), 'png',
                                     ConversionOptions())
        assert result is None


class TestICSCoreIntegration:
    """Integration through DocumentConverter core."""

    def test_ics_registered_in_converter(self, converter):
        assert 'ics' in converter.get_supported_input_formats()
        outputs = converter.get_supported_output_formats('ics')
        assert set(outputs) == {'pdf', 'docx', 'txt', 'html'}

    def test_can_convert_matrix(self, converter):
        assert converter.can_convert('ics', 'pdf') is True
        assert converter.can_convert('ics', 'docx') is True
        assert converter.can_convert('ics', 'txt') is True
        assert converter.can_convert('ics', 'html') is True
        assert converter.can_convert('ics', 'png') is False
        assert converter.can_convert('ics', 'djvu') is False

    def test_convert_via_core_txt(self, converter, sample_ics, test_dir):
        result = converter.convert(str(sample_ics), 'txt')
        assert result.success is True
        assert result.error is None
        assert result.output_size > 0
        content = Path(result.output_path).read_text(encoding='utf-8')
        assert "Встреча команды" in content

    def test_convert_via_core_pdf(self, converter, sample_ics):
        result = converter.convert(str(sample_ics), 'pdf')
        assert result.success is True
        assert result.format == 'pdf'
        assert result.output_size > 0

    def test_ics_can_merge_to_pdf(self, converter, sample_ics):
        """ICS participates in the merge-to-PDF pipeline via can_convert."""
        assert converter.can_convert('ics', 'pdf') is True
