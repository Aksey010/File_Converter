"""ICS (iCalendar) format handler.

Parses .ics calendar files and renders the events as TXT, HTML, PDF or DOCX.

Parsing strategy:
1. If the optional ``icalendar`` package is installed, it is used (best
   fidelity: escaped values, timezones, recurrence rules, grouped params).
2. Otherwise a built-in RFC 5545-ish fallback parser is used, so ICS
   conversion works with zero additional dependencies.
"""

import html as html_module
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

from .base import BaseHandler
from ..core import ConversionOptions
from ..libraries import get_libraries


# Fonts with Cyrillic (and other non-latin) coverage, tried in order.
_UNICODE_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    "C:/Windows/Fonts/cour.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]

_STATUS_RU = {
    "CONFIRMED": "Подтверждено",
    "TENTATIVE": "Под вопросом",
    "CANCELLED": "Отменено",
}


class ICSHandler(BaseHandler):
    """Handler for ICS (iCalendar) files."""

    def _get_docx(self):
        return self._libs.get_docx()

    def convert(
        self,
        input_path: str,
        output_path: str,
        output_format: str,
        options: ConversionOptions
    ) -> Optional[str]:
        """Convert ICS to target format."""
        output_format = output_format.lower()

        calendar_name, events = self._parse_events(input_path)

        if output_format == 'txt':
            return self._events_to_txt(calendar_name, events, output_path)
        elif output_format == 'html':
            return self._events_to_html(calendar_name, events, output_path)
        elif output_format == 'pdf':
            return self._events_to_pdf(calendar_name, events, output_path)
        elif output_format == 'docx':
            return self._events_to_docx(calendar_name, events, output_path)

        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_events(self, input_path: str) -> Tuple[str, List[dict]]:
        """Parse ICS file → (calendar name, list of event dicts sorted by start)."""
        with open(input_path, 'rb') as f:
            raw = f.read()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('cp1252', errors='replace')

        calendar_cls = self._libs.get_icalendar()
        if calendar_cls is not None:
            try:
                return self._parse_with_icalendar(calendar_cls, text)
            except Exception:
                pass  # fall back to the built-in parser
        return self._parse_builtin(text)

    # --- icalendar-based parsing (best fidelity) ---

    def _parse_with_icalendar(self, calendar_cls, text: str) -> Tuple[str, List[dict]]:
        calendar = calendar_cls.from_ical(text)
        name = ''
        name_prop = calendar.get('X-WR-CALNAME')
        if name_prop is not None:
            name = str(name_prop)

        events = []
        for item in calendar.walk('VEVENT'):
            start, start_all_day, start_tz = self._ical_dt(item.get('DTSTART'))
            end, end_all_day, end_tz = self._ical_dt(item.get('DTEND'))

            organizer = ''
            org = item.get('ORGANIZER')
            if org is not None:
                cn = str(org.params.get('CN', '')).strip()
                addr = str(org).replace('mailto:', '').strip()
                organizer = f"{cn} <{addr}>" if cn else addr

            attendees = []
            att = item.get('ATTENDEE')
            if att is not None:
                if not isinstance(att, list):
                    att = [att]
                for a in att:
                    cn = str(a.params.get('CN', '')).strip()
                    addr = str(a).replace('mailto:', '').strip()
                    attendees.append(f"{cn} <{addr}>" if cn else addr)

            rrule = ''
            rr = item.get('RRULE')
            if rr is not None:
                freq = rr.get('FREQ')
                if isinstance(freq, (list, tuple)):
                    freq = freq[0] if freq else None
                rrule = str(freq).lower() if freq else str(rr)

            events.append({
                'summary': str(item.get('SUMMARY', '') or ''),
                'start': start,
                'start_all_day': start_all_day,
                'start_tz': start_tz,
                'end': end,
                'end_all_day': end_all_day,
                'end_tz': end_tz,
                'location': str(item.get('LOCATION', '') or ''),
                'description': str(item.get('DESCRIPTION', '') or ''),
                'organizer': organizer,
                'attendees': attendees,
                'status': str(item.get('STATUS', '') or ''),
                'rrule': rrule,
            })
        return name, self._sorted(events)

    def _ical_dt(self, prop):
        """Extract (value, all_day, tz_name) from an icalendar DTSTART/DTEND prop."""
        if prop is None:
            return None, False, None
        value = prop.dt
        tz = str(prop.params.get('TZID', '') or '') or None
        if isinstance(value, datetime):
            return value, False, tz
        if isinstance(value, date):
            return value, True, None
        return None, False, tz

    # --- built-in fallback parsing (no external dependencies) ---

    @staticmethod
    def _unfold_lines(text: str) -> List[str]:
        """RFC 5545 line unfolding: continuation lines start with space/tab."""
        lines = []
        for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
            if raw[:1] in (' ', '\t') and lines:
                lines[-1] += raw[1:]
            else:
                lines.append(raw)
        return lines

    @staticmethod
    def _split_name_value(line: str) -> Tuple[str, str]:
        """Split 'NAME;PARAM=..:value' at the first ':' outside double quotes."""
        in_quotes = False
        for i, ch in enumerate(line):
            if ch == '"':
                in_quotes = not in_quotes
            elif ch == ':' and not in_quotes:
                return line[:i], line[i + 1:]
        return line, ''

    @staticmethod
    def _parse_prop(name_part: str) -> Tuple[str, dict]:
        pieces = name_part.split(';')
        prop_name = pieces[0].strip().upper()
        params = {}
        for piece in pieces[1:]:
            if '=' in piece:
                k, v = piece.split('=', 1)
                params[k.strip().upper()] = v.strip().strip('"')
        return prop_name, params

    @staticmethod
    def _unescape(value: str) -> str:
        out = []
        i = 0
        while i < len(value):
            ch = value[i]
            if ch == '\\' and i + 1 < len(value):
                nxt = value[i + 1]
                if nxt in ('n', 'N'):
                    out.append('\n')
                else:
                    out.append(nxt)
                i += 2
            else:
                out.append(ch)
                i += 1
        return ''.join(out)

    @staticmethod
    def _parse_dt_value(value: str, params: dict) -> Tuple[Optional[object], bool, Optional[str]]:
        """Parse a DATE or DATE-TIME value → (date/datetime/None, all_day, tz)."""
        value = value.strip()
        if params.get('VALUE', '').upper() == 'DATE' or re.fullmatch(r'\d{8}', value):
            m = re.fullmatch(r'(\d{4})(\d{2})(\d{2})', value)
            if m:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), True, None
            return None, True, None

        m = re.fullmatch(r'(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)', value)
        if not m:
            return None, False, None
        y, mo, d, h, mi, s, z = m.groups()
        tzinfo = timezone.utc if z == 'Z' else None
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=tzinfo)
        tz = None if z == 'Z' else (params.get('TZID') or None)
        return dt, False, tz

    def _parse_builtin(self, text: str) -> Tuple[str, List[dict]]:
        name = ''
        events: List[dict] = []
        current: Optional[dict] = None

        for line in self._unfold_lines(text):
            if not line.strip():
                continue
            name_part, value = self._split_name_value(line)
            prop_name, params = self._parse_prop(name_part)

            if prop_name == 'BEGIN' and value.strip().upper() == 'VEVENT':
                current = {
                    'summary': '', 'start': None, 'start_all_day': False, 'start_tz': None,
                    'end': None, 'end_all_day': False, 'end_tz': None,
                    'location': '', 'description': '', 'organizer': '',
                    'attendees': [], 'status': '', 'rrule': '',
                }
                continue
            if prop_name == 'END' and value.strip().upper() == 'VEVENT':
                if current is not None:
                    events.append(current)
                current = None
                continue
            if current is None:
                if prop_name == 'X-WR-CALNAME' and not name:
                    name = self._unescape(value).strip()
                continue

            if prop_name == 'SUMMARY':
                current['summary'] = self._unescape(value)
            elif prop_name == 'LOCATION':
                current['location'] = self._unescape(value)
            elif prop_name == 'DESCRIPTION':
                current['description'] = self._unescape(value)
            elif prop_name == 'STATUS':
                current['status'] = value.strip().upper()
            elif prop_name == 'RRULE':
                freq = re.search(r'FREQ=([A-Z]+)', value)
                current['rrule'] = freq.group(1).lower() if freq else value
            elif prop_name == 'DTSTART':
                dt, all_day, tz = self._parse_dt_value(value, params)
                current['start'], current['start_all_day'], current['start_tz'] = dt, all_day, tz
            elif prop_name == 'DTEND':
                dt, all_day, tz = self._parse_dt_value(value, params)
                current['end'], current['end_all_day'], current['end_tz'] = dt, all_day, tz
            elif prop_name == 'ORGANIZER':
                cn = params.get('CN', '').strip()
                addr = value.replace('mailto:', '').strip()
                current['organizer'] = f"{cn} <{addr}>" if cn else addr
            elif prop_name == 'ATTENDEE':
                cn = params.get('CN', '').strip()
                addr = value.replace('mailto:', '').strip()
                current['attendees'].append(f"{cn} <{addr}>" if cn else addr)

        return name, self._sorted(events)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sorted(events: List[dict]) -> List[dict]:
        """Sort events by start time (events without start go last).

        Timezone-aware datetimes are normalised to UTC-naive so they can be
        compared with naive (floating) times without raising TypeError.
        """
        def key(ev):
            start = ev['start']
            if start is None:
                return datetime.max
            if isinstance(start, datetime):
                if start.tzinfo is not None:
                    return start.astimezone(timezone.utc).replace(tzinfo=None)
                return start
            return datetime(start.year, start.month, start.day)
        return sorted(events, key=key)

    @staticmethod
    def _format_when(value, all_day: bool, tz: Optional[str]) -> str:
        if value is None:
            return '—'
        if all_day:
            return f"{value.day:02d}.{value.month:02d}.{value.year} (весь день)"
        text = f"{value.day:02d}.{value.month:02d}.{value.year} {value.hour:02d}:{value.minute:02d}"
        if value.tzinfo is not None:
            offset = value.utcoffset() or timedelta(0)
            if offset == timedelta(0):
                text += " UTC"
            else:
                total = int(offset.total_seconds())
                sign = '+' if total >= 0 else '-'
                hours, rem = divmod(abs(total), 3600)
                minutes = rem // 60
                text += f" (UTC{sign}{hours:02d}:{minutes:02d})"
        elif tz:
            text += f" ({tz})"
        return text

    @classmethod
    def _status_ru(cls, status: str) -> str:
        return _STATUS_RU.get(status.upper(), status) if status else ''

    def _event_lines(self, index: int, ev: dict) -> List[str]:
        """Human-readable field lines for one event."""
        lines = [f"{index}. {ev['summary'] or '(без названия)'}"]
        when = self._format_when(ev['start'], ev['start_all_day'], ev['start_tz'])
        lines.append(f"   Когда: {when}")
        if ev['end'] is not None:
            until = self._format_when(ev['end'], ev['end_all_day'], ev['end_tz'])
            lines.append(f"   До: {until}")
        if ev['location']:
            lines.append(f"   Где: {ev['location']}")
        if ev['description']:
            desc_lines = ev['description'].split('\n')
            lines.append(f"   Описание: {desc_lines[0]}")
            lines.extend(f"   {dl}" for dl in desc_lines[1:])
        if ev['organizer']:
            lines.append(f"   Организатор: {ev['organizer']}")
        if ev['attendees']:
            lines.append(f"   Участники: {', '.join(ev['attendees'])}")
        if ev['rrule']:
            lines.append(f"   Повторяется: {ev['rrule']}")
        status_ru = self._status_ru(ev['status'])
        if status_ru:
            lines.append(f"   Статус: {status_ru}")
        return lines

    def _build_blocks(self, calendar_name: str, events: List[dict]) -> List[str]:
        """Text blocks: header + one block per event."""
        header = []
        title = calendar_name or "События календаря"
        header.append(title)
        header.append(f"Всего событий: {len(events)}")
        blocks = ["\n".join(header)]
        for i, ev in enumerate(events, 1):
            blocks.append("\n".join(self._event_lines(i, ev)))
        return blocks

    # ------------------------------------------------------------------
    # Renderers
    # ------------------------------------------------------------------

    def _events_to_txt(self, calendar_name: str, events: List[dict], output_path: str) -> str:
        if not events:
            raise RuntimeError("ICS file contains no events")
        blocks = self._build_blocks(calendar_name, events)
        text = "\n\n".join(blocks) + "\n"
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text)
        return output_path

    def _events_to_html(self, calendar_name: str, events: List[dict], output_path: str) -> str:
        if not events:
            raise RuntimeError("ICS file contains no events")

        title = calendar_name or "События календаря"

        def esc(value: str) -> str:
            return html_module.escape(str(value))

        cards = []
        for i, ev in enumerate(events, 1):
            when = esc(self._format_when(ev['start'], ev['start_all_day'], ev['start_tz']))
            rows = [f"<p><strong>Когда:</strong> {when}</p>"]
            if ev['end'] is not None:
                until = esc(self._format_when(ev['end'], ev['end_all_day'], ev['end_tz']))
                rows.append(f"<p><strong>До:</strong> {until}</p>")
            if ev['location']:
                rows.append(f"<p><strong>Где:</strong> {esc(ev['location'])}</p>")
            if ev['description']:
                desc_html = '<br>'.join(esc(line) for line in ev['description'].split('\n'))
                rows.append(f"<p><strong>Описание:</strong> {desc_html}</p>")
            if ev['organizer']:
                rows.append(f"<p><strong>Организатор:</strong> {esc(ev['organizer'])}</p>")
            if ev['attendees']:
                rows.append(f"<p><strong>Участники:</strong> {esc(', '.join(ev['attendees']))}</p>")
            if ev['rrule']:
                rows.append(f"<p><strong>Повторяется:</strong> {esc(ev['rrule'])}</p>")
            status_ru = esc(self._status_ru(ev['status']))
            if status_ru:
                rows.append(f"<p><strong>Статус:</strong> {status_ru}</p>")

            cards.append(
                '<div class="event">\n'
                f'  <h2>{i}. {esc(ev["summary"] or "(без названия)")}</h2>\n'
                + '\n'.join(rows) +
                '\n</div>'
            )

        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{esc(title)}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 2cm; line-height: 1.5; }}
        h1 {{ border-bottom: 2px solid #333; padding-bottom: 8px; }}
        .event {{ border: 1px solid #ccc; border-radius: 8px; padding: 12px 16px; margin: 14px 0; }}
        .event h2 {{ margin-top: 0; font-size: 1.1em; }}
        p {{ margin: 4px 0; }}
    </style>
</head>
<body>
<h1>{esc(title)}</h1>
<p>Всего событий: {len(events)}</p>
{chr(10).join(cards)}
</body>
</html>"""

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        return output_path

    def _events_to_pdf(self, calendar_name: str, events: List[dict], output_path: str) -> str:
        if not events:
            raise RuntimeError("ICS file contains no events")

        fitz = self._get_fitz()
        if not fitz:
            raise RuntimeError("PyMuPDF (fitz) not installed")

        blocks = self._build_blocks(calendar_name, events)
        doc = fitz.open()
        try:
            width, height, margin = 595, 842, 57
            full_rect = fitz.Rect(margin, margin, width - margin, height - margin)
            page = doc.new_page(width=width, height=height)
            fontname = self._register_unicode_font(page, fitz)
            cur_rect = full_rect

            for block in blocks:
                chunk = block + "\n"
                while chunk:
                    if cur_rect.height < 40:
                        page = doc.new_page(width=width, height=height)
                        self._register_unicode_font(page, fitz)
                        cur_rect = full_rect
                    rc = page.insert_textbox(
                        cur_rect, chunk, fontsize=11, fontname=fontname,
                        lineheight=1.35, align=0
                    )
                    if rc >= 0:
                        used = cur_rect.height - rc
                        cur_rect.y0 += used + 6
                        chunk = ""
                    elif cur_rect.y0 - full_rect.y0 <= 1:
                        # Doesn't fit even on an empty page → hard-split.
                        fit_chars = max(200, len(chunk) // 2)
                        rc = -1
                        while rc < 0 and fit_chars >= 50:
                            rc = page.insert_textbox(
                                cur_rect, chunk[:fit_chars], fontsize=11,
                                fontname=fontname, lineheight=1.35, align=0
                            )
                            if rc < 0:
                                fit_chars = int(fit_chars * 0.8)
                        if rc < 0:
                            raise RuntimeError("Failed to render event text into PDF page")
                        used = cur_rect.height - rc
                        cur_rect.y0 += used + 6
                        chunk = chunk[fit_chars:]
                    else:
                        page = doc.new_page(width=width, height=height)
                        self._register_unicode_font(page, fitz)
                        cur_rect = full_rect

            doc.save(output_path, garbage=3, deflate=True)
        finally:
            doc.close()
        return output_path

    @staticmethod
    def _register_unicode_font(page, fitz) -> str:
        """Register a font with Cyrillic support; returns fontname for insert_textbox."""
        for path in _UNICODE_FONT_CANDIDATES:
            if os.path.exists(path):
                try:
                    page.insert_font(fontname="uni", fontfile=path)
                    return "uni"
                except Exception:
                    continue
        try:
            font = fitz.Font("cjk")  # MuPDF embedded fallback, covers Cyrillic
            page.insert_font(fontname="uni", fontbuffer=font.buffer)
            return "uni"
        except Exception:
            return "helv"

    def _events_to_docx(self, calendar_name: str, events: List[dict], output_path: str) -> str:
        if not events:
            raise RuntimeError("ICS file contains no events")

        docx_cls = self._get_docx()
        if not docx_cls:
            raise RuntimeError("python-docx not installed")

        doc = docx_cls()
        doc.add_heading(calendar_name or "События календаря", level=1)
        doc.add_paragraph(f"Всего событий: {len(events)}")

        for i, ev in enumerate(events, 1):
            doc.add_heading(f"{i}. {ev['summary'] or '(без названия)'}", level=2)
            when = self._format_when(ev['start'], ev['start_all_day'], ev['start_tz'])
            doc.add_paragraph(f"Когда: {when}")
            if ev['end'] is not None:
                until = self._format_when(ev['end'], ev['end_all_day'], ev['end_tz'])
                doc.add_paragraph(f"До: {until}")
            if ev['location']:
                doc.add_paragraph(f"Где: {ev['location']}")
            if ev['description']:
                desc_lines = ev['description'].split('\n')
                doc.add_paragraph(f"Описание: {desc_lines[0]}")
                for extra_line in desc_lines[1:]:
                    doc.add_paragraph(extra_line)
            if ev['organizer']:
                doc.add_paragraph(f"Организатор: {ev['organizer']}")
            if ev['attendees']:
                doc.add_paragraph(f"Участники: {', '.join(ev['attendees'])}")
            if ev['rrule']:
                doc.add_paragraph(f"Повторяется: {ev['rrule']}")
            status_ru = self._status_ru(ev['status'])
            if status_ru:
                doc.add_paragraph(f"Статус: {status_ru}")

        doc.save(output_path)
        return output_path
