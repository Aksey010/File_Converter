"""Tests for DJVU handler (tool-independent paths are mocked).

Real DJVU conversion requires djvulibre (ddjvu) on the system; these tests
mock subprocess.run so the handler logic is verified on any machine.
"""

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from converter import ConversionOptions
from converter.formats.djvu_handler import DJVUHandler


class TestDJVUToolDetection:
    """Regression tests for tool-availability detection.

    Real djvulibre 3.5.x (Windows) returns a non-zero exit code for
    'ddjvu --version' (unknown option) and 'djvudump --version' treats the
    flag as a filename. A tool must count as available whenever its
    executable can be launched at all — not only when the probe exits 0.
    """

    @staticmethod
    def _run_returns_code(code):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, code, stdout='', stderr='usage')
        return fake_run

    @staticmethod
    def _run_raises_missing(cmd, **kwargs):
        raise FileNotFoundError(2, 'The system cannot find the file specified')

    def test_ddjvu_detected_with_nonzero_probe_exit(self, monkeypatch):
        """ddjvu with a non-zero probe exit code (real 3.5.x behavior) is available."""
        monkeypatch.setattr(
            'converter.formats.djvu_handler.subprocess.run',
            self._run_returns_code(10),
        )
        handler = DJVUHandler()
        assert handler._ddjvu_available is True

    def test_djvudump_detected_with_nonzero_probe_exit(self, monkeypatch):
        """djvudump with a non-zero probe exit code (real 3.5.x behavior) is available."""
        monkeypatch.setattr(
            'converter.formats.djvu_handler.subprocess.run',
            self._run_returns_code(1),
        )
        handler = DJVUHandler()
        assert handler._djvudump_available is True

    def test_ddjvu_missing_when_file_not_found(self, monkeypatch):
        monkeypatch.setattr(
            'converter.formats.djvu_handler.subprocess.run',
            self._run_raises_missing,
        )
        handler = DJVUHandler()
        assert handler._ddjvu_available is False
        assert handler._djvudump_available is False

    def test_probe_timeout_still_means_found(self, monkeypatch):
        """A hung probe means the program exists — treat it as available."""

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 10)

        monkeypatch.setattr(
            'converter.formats.djvu_handler.subprocess.run', fake_run
        )
        handler = DJVUHandler()
        assert handler._ddjvu_available is True
        assert handler._djvudump_available is True



def _fake_ddjvu_tiff_factory(page_count=2):
    """Return a subprocess.run replacement that 'renders' a multi-page TIFF."""

    def fake_run(cmd, **kwargs):
        if cmd[0] != 'ddjvu':
            raise AssertionError(f"Unexpected command: {cmd}")
        out = Path(cmd[-1])
        pages = [Image.new('RGB', (120, 160), 'white') for _ in range(page_count)]
        pages[0].save(out, save_all=True, append_images=pages[1:])
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

    return fake_run


class TestDJVUHandlerPaths:
    """Tests for handler logic that does not require ddjvu on the system."""

    def test_ddjvu_to_pdf_via_images(self, djvu_handler, test_dir, monkeypatch):
        """Fallback path DJVU -> TIFF -> PDF works (regression: missing io import)."""
        monkeypatch.setattr(
            'converter.formats.djvu_handler.subprocess.run', _fake_ddjvu_tiff_factory()
        )
        # input file only needs to exist; content is read by (mocked) ddjvu
        input_path = test_dir / "input.djvu"
        input_path.write_bytes(b'FORM\x00\x00\x00\x04DJVU')

        import fitz
        output_path = test_dir / "out_via_images.pdf"
        result = djvu_handler._djvu_to_pdf_via_images(
            str(input_path), str(output_path), ConversionOptions()
        )

        assert result == str(output_path)
        assert output_path.exists() and output_path.stat().st_size > 0
        doc = fitz.open(str(output_path))
        assert doc.page_count == 2  # both mocked TIFF pages landed in the PDF
        doc.close()

    def test_ddjvu_command_uses_supported_options(self, djvu_handler, test_dir, monkeypatch):
        """ddjvu must be called only with options real djvulibre supports.

        Regression: the handler used to pass '-dpi=N' (no such ddjvu option
        in any build) and '-page N-M' (separate form not accepted; only
        '-page=N-M' works).
        """
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['cmd'] = list(cmd)
            out = Path(cmd[-1])
            Image.new('RGB', (120, 160), 'white').save(out)
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        monkeypatch.setattr('converter.formats.djvu_handler.subprocess.run', fake_run)

        input_path = test_dir / "input.djvu"
        input_path.write_bytes(b'FORM\x00\x00\x00\x04DJVU')
        output_path = test_dir / "out.png"
        djvu_handler._djvu_to_images(
            str(input_path), str(output_path), 'png',
            ConversionOptions(dpi=150, page_range="2-3")
        )

        cmd = captured['cmd']
        assert cmd[0] == 'ddjvu'
        assert not any(arg.startswith('-dpi') for arg in cmd)
        assert '-scale=50' in cmd  # dpi 150 → 50% of natural resolution
        assert '-page=2-3' in cmd

    def test_convert_raises_without_ddjvu(self, monkeypatch, test_dir):
        """Without ddjvu, convert() explains what to install."""
        handler = DJVUHandler()
        handler._ddjvu_available = False
        with pytest.raises(RuntimeError, match="djvulibre"):
            handler.convert(
                str(test_dir / "any.djvu"), str(test_dir / "out.pdf"), 'pdf',
                ConversionOptions()
            )
