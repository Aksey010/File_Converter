"""DJVU format handler."""

import io
import os
import tempfile
import subprocess
from typing import Optional, List
from .base import BaseHandler
from ..core import ConversionOptions
from ..libraries import get_libraries


class DJVUHandler(BaseHandler):
    """Handler for DJVU/DJV files."""
    
    def __init__(self):
        self._libs = get_libraries()
        self._ddjvu_available = False
        self._djvudump_available = False
        self._check_tools()
    
    def _check_tools(self):
        """Check for DJVU tools.

        A tool counts as available whenever its executable can be launched at
        all. The exit code of a probe call is deliberately ignored: real
        djvulibre 3.5.x builds return non-zero for 'ddjvu --version' (unknown
        option) and 'djvudump --version' treats the flag as a filename, so a
        returncode-based check falsely reports installed tools as missing.
        """
        for attr, tool in (
            ('_ddjvu_available', 'ddjvu'),
            ('_djvudump_available', 'djvudump'),
        ):
            try:
                # No-arg call prints usage and exits; we only care that the
                # executable was found on PATH.
                subprocess.run([tool], capture_output=True, text=True, timeout=15)
                setattr(self, attr, True)
            except FileNotFoundError:
                setattr(self, attr, False)
            except Exception:
                # Program exists but the probe misbehaved (timeout,
                # permissions, ...). Treat as present — the actual conversion
                # will surface any real problem with a clear error.
                setattr(self, attr, True)
    
    def _get_pillow(self):
        return self._libs.get_pillow()
    
    def _get_fitz(self):
        return self._libs.get_fitz()
    
    def _get_pytesseract(self):
        return self._libs.get_pytesseract()
    
    def convert(
        self,
        input_path: str,
        output_path: str,
        output_format: str,
        options: ConversionOptions
    ) -> Optional[str]:
        """Convert DJVU to target format."""
        output_format = output_format.lower()
        
        if not self._ddjvu_available:
            raise RuntimeError(
                "DJVU conversion requires djvulibre (ddjvu command). "
                "Install it from https://djvu.sourceforge.net/"
            )
        
        if output_format == 'pdf':
            return self._djvu_to_pdf(input_path, output_path, options)
        elif output_format in ('png', 'jpeg', 'jpg', 'tiff', 'bmp', 'webp'):
            return self._djvu_to_images(input_path, output_path, output_format, options)
        elif output_format == 'txt':
            return self._djvu_to_text(input_path, output_path, options)
        
        return None

    @staticmethod
    def _dpi_to_scale(dpi: int) -> str:
        """Convert the app DPI option to a ddjvu ``-scale`` percentage.

        ddjvu has no ``-dpi`` option (any djvulibre build). It renders pages
        at their natural resolution — typically 300 dpi for scanned documents
        — and ``-scale=N`` takes an integer percentage (1..999): DPI 300 →
        100 (natural size), 150 → 50 (half), 600 → 200 (double).
        """
        scale_pct = max(1, min(999, round(dpi / 300.0 * 100)))
        return f"-scale={scale_pct}"
    
    def _djvu_to_pdf(self, input_path: str, output_path: str, options: ConversionOptions) -> Optional[str]:
        """Convert DJVU to PDF using ddjvu."""
        # ddjvu can output PDF directly
        cmd = [
            'ddjvu',
            '-format=pdf',
            f'-quality={options.djvu_quality}',
            self._dpi_to_scale(options.dpi),
            input_path,
            output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode != 0:
            # Try alternative: convert to images then to PDF
            return self._djvu_to_pdf_via_images(input_path, output_path, options)
        
        return output_path if os.path.exists(output_path) else None
    
    def _djvu_to_pdf_via_images(self, input_path: str, output_path: str, options: ConversionOptions) -> Optional[str]:
        """Fallback: DJVU -> Images -> PDF."""
        pillow = self._get_pillow()
        fitz = self._get_fitz()
        
        if not pillow or not fitz:
            raise RuntimeError("Pillow and PyMuPDF required for fallback")
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Convert to TIFF (multi-page)
            tiff_path = os.path.join(tmpdir, 'output.tiff')
            cmd = [
                'ddjvu',
                '-format=tiff',
                f'-quality={options.djvu_quality}',
                self._dpi_to_scale(options.dpi),
                input_path,
                tiff_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0 or not os.path.exists(tiff_path):
                raise RuntimeError(f"ddjvu failed: {result.stderr}")
            
            # Open TIFF and save as PDF
            img = pillow.open(tiff_path)
            pdf_doc = fitz.open()
            
            try:
                page_num = 0
                while True:
                    img.seek(page_num)
                    page_img = img.copy()
                    page_img = self._apply_image_options(page_img, options)
                    
                    # Convert to PDF page
                    buf = io.BytesIO()
                    page_img.save(buf, format='PNG')
                    pdf_page = pdf_doc.new_page(width=page_img.width, height=page_img.height)
                    pdf_page.insert_image(pdf_page.rect, stream=buf.getvalue())
                    
                    page_num += 1
            except EOFError:
                pass
            finally:
                img.close()
            
            pdf_doc.save(output_path, garbage=3, deflate=True)
            pdf_doc.close()
        
        return output_path
    
    def _djvu_to_images(
        self,
        input_path: str,
        output_path: str,
        output_format: str,
        options: ConversionOptions
    ) -> Optional[str]:
        """Convert DJVU to images."""
        # ddjvu -format supports only: pbm,pgm,ppm,pnm,rle,tiff,pdf.
        # Everything else is rendered as PPM first, then re-encoded by Pillow.
        fmt_map = {
            'png': 'ppm',
            'jpeg': 'ppm',
            'jpg': 'ppm',
            'tiff': 'tiff',
            'bmp': 'ppm',
            'webp': 'ppm',
        }
        
        ddjvu_format = fmt_map.get(output_format, 'ppm')
        needs_reencode = ddjvu_format != 'tiff'
        
        if needs_reencode and not self._get_pillow():
            raise RuntimeError(f"Pillow is required for DJVU → {output_format.upper()} output")
        
        # Parse page range
        page_range = self._parse_page_range(options.page_range)
        page_args = []
        if page_range:
            # ddjvu only accepts the clustered form: -page=PAGESPEC
            page_args = [f'-page={page_range[0]}-{page_range[1]}']
        
        cmd = [
            'ddjvu',
            f'-format={ddjvu_format}',
            f'-quality={options.djvu_quality}',
            self._dpi_to_scale(options.dpi),
        ] + page_args + [input_path, output_path]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode != 0:
            raise RuntimeError(f"ddjvu failed: {result.stderr}")
        
        # Re-encode PPM/PNM into the requested format. The intermediate is
        # loaded into memory first: writing to the same path while PIL still
        # holds it open fails on Windows.
        if needs_reencode:
            pillow = self._get_pillow()
            with open(output_path, 'rb') as f:
                raw = f.read()
            img = pillow.open(io.BytesIO(raw))
            try:
                img = self._apply_image_options(img, options)
                self._save_image(img, output_path, output_format, options)
            finally:
                img.close()
        
        return output_path if os.path.exists(output_path) else None
    
    def _djvu_to_text(self, input_path: str, output_path: str, options: ConversionOptions) -> Optional[str]:
        """Extract text from DJVU using djvudump or OCR."""
        # Try djvudump first (extracts hidden text layer)
        if self._djvudump_available:
            try:
                result = subprocess.run(
                    ['djvudump', input_path],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode == 0 and result.stdout.strip():
                    with open(output_path, 'w', encoding='utf-8') as f:
                        f.write(result.stdout)
                    return output_path
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        
        # Fallback: convert to images and OCR (requires tesseract)
        pytesseract = self._get_pytesseract()
        pillow = self._get_pillow()
        
        if pytesseract and pillow:
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    # Convert first few pages to images
                    tiff_path = os.path.join(tmpdir, 'ocr.tiff')
                    cmd = [
                        'ddjvu',
                        '-format=tiff',
                        f'-dpi={max(options.dpi, 300)}',  # Higher DPI for OCR
                        '-page=1-10',  # Limit pages for OCR
                        input_path,
                        tiff_path
                    ]
                    
                    subprocess.run(cmd, capture_output=True, timeout=300)
                    
                    if os.path.exists(tiff_path):
                        img = pillow.open(tiff_path)
                        text_parts = []
                        
                        try:
                            page_num = 0
                            while True:
                                img.seek(page_num)
                                page_text = pytesseract.image_to_string(img, lang='rus+eng')
                                if page_text.strip():
                                    text_parts.append(page_text)
                                page_num += 1
                        except EOFError:
                            pass
                        finally:
                            img.close()
                        
                        with open(output_path, 'w', encoding='utf-8') as f:
                            f.write('\n\n'.join(text_parts))
                        
                        return output_path
            except ImportError:
                pass
        
        # Last resort: just note that text extraction needs OCR
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("[Text extraction from DJVU requires either:\n"
                    "1. djvudump (from djvulibre) for hidden text layer\n"
                    "2. tesseract-ocr + pytesseract for OCR]\n")
        return output_path
    
    def _parse_page_range(self, page_range: Optional[str]) -> Optional[tuple]:
        """Parse page range string like '1-5'."""
        if not page_range:
            return None
        if '-' in page_range and ',' not in page_range:
            parts = page_range.split('-')
            try:
                return (int(parts[0]), int(parts[1]))
            except ValueError:
                return None
        return None