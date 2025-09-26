import os
from pathlib import Path
from typing import Iterable, List, Tuple

from django.core.management.base import BaseCommand
from django.db import transaction

from ebooklib import epub
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pdfminer.layout import LAParams
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.converter import HTMLConverter
from io import BytesIO
from pathlib import Path
from django.conf import settings
from PIL import Image
import os

from standards.models import Standard, Page


class Command(BaseCommand):
    help = "Ingest PDFs and EPUB into Standard/Page tables"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--base_dir", default=str(Path.cwd()), help="Directory containing the source files")
        parser.add_argument("--rebuild", action="store_true", help="Drop existing Page rows for files and re-ingest")

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        base_dir = Path(options["base_dir"])  # type: ignore[index]
        files = list(base_dir.glob("*.pdf")) + list(base_dir.glob("*.epub"))
        if not files:
            self.stdout.write(self.style.WARNING("No PDF/EPUB files found to ingest."))
            return

        for fpath in files:
            if fpath.suffix.lower() == ".pdf":
                title = fpath.stem
                std, _ = Standard.objects.get_or_create(title=title, defaults={"file_path": str(fpath), "source_type": "pdf"})
                if options["rebuild"]:
                    Page.objects.filter(standard=std).delete()
                self._ingest_pdf(std, fpath)
            elif fpath.suffix.lower() == ".epub":
                title = fpath.stem
                std, _ = Standard.objects.get_or_create(title=title, defaults={"file_path": str(fpath), "source_type": "epub"})
                if options["rebuild"]:
                    Page.objects.filter(standard=std).delete()
                self._ingest_epub(std, fpath)

        self.stdout.write(self.style.SUCCESS("Ingestion complete."))

    @transaction.atomic
    def _ingest_pdf(self, standard: Standard, path: Path) -> None:
        rsrcmgr = PDFResourceManager()
        laparams = LAParams(line_margin=0.2, word_margin=0.1)
        with open(str(path), 'rb') as fp:
            interpreter = PDFPageInterpreter(rsrcmgr, None)  # will be set per-page
            for idx, page in enumerate(PDFPage.get_pages(fp)):
                outfp = BytesIO()
                device = HTMLConverter(rsrcmgr, outfp, laparams=laparams)
                interpreter.device = device
                interpreter.process_page(page)
                device.close()
                html_full = outfp.getvalue().decode('utf-8', errors='ignore')
                soup = BeautifulSoup(html_full, 'lxml')
                body = soup.body or soup
                for tag in body.find_all(['script', 'style']):
                    tag.decompose()
                content_html = str(body)
                content_text = body.get_text("\n", strip=False)
                Page.objects.create(
                    standard=standard,
                    page_index=idx,
                    content=content_text,
                    content_html=content_html,
                )

    @transaction.atomic
    def _ingest_epub(self, standard: Standard, path: Path) -> None:
        book = epub.read_epub(str(path))
        texts: List[str] = []
        page_idx = 0
        for item in book.get_items():
            # 9 corresponds to DOCUMENT type in ebooklib
            if getattr(item, 'media_type', '').endswith('html') or item.get_type() == 9:
                soup = BeautifulSoup(item.get_content(), "lxml")
                # Keep headings, lists: minimal HTML
                for tag in soup(["script", "style"]):
                    tag.decompose()
                # Store HTML in chunks of roughly pages
                body = soup.body or soup
                # Virtual pagination: split by block elements into ~1200-1600 char chunks
                html_chunks: List[str] = []
                current: List[str] = []
                acc_len = 0
                max_len = 1600
                min_len = 800
                block_tags = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "pre", "blockqcouote", "img", "div"}
                for child in list(body.children):
                    if getattr(child, 'name', None) is None:
                        # NavigableString
                        text_piece = str(child)
                        if text_piece.strip():
                            current.append(text_piece)
                            acc_len += len(text_piece)
                        continue
                    # Tag
                    html_piece = str(child)
                    current.append(html_piece)
                    acc_len += len(child.get_text(" ", strip=False))
                    if child.name in block_tags and acc_len >= min_len:
                        html_chunks.append("".join(current))
                        current = []
                        acc_len = 0
                if current:
                    html_chunks.append("".join(current))

                if not html_chunks:
                    html_chunks = [str(body)]

                for chunk in html_chunks:
                    text = BeautifulSoup(chunk, "lxml").get_text("\n", strip=False)
                    texts.append(text)
                    Page.objects.create(
                        standard=standard,
                        page_index=page_idx,
                        content=text,
                        content_html=chunk,
                    )
                    page_idx += 1
        # If no items captured (rare), fallback to chunking concatenated text
        if not standard.pages.exists():
            full_text = "\n\n".join(texts)
            chunks = self._split_text(full_text)
            for idx, chunk in enumerate(chunks):
                Page.objects.create(standard=standard, page_index=idx, content=chunk)

    def _split_text(self, text: str) -> List[str]:
        tokens = text.split()
        chunk_size = 400
        chunks: List[str] = []
        current: List[str] = []
        for tok in tokens:
            current.append(tok)
            if len(current) >= chunk_size:
                chunks.append(" ".join(current))
                current = []
        if current:
            chunks.append(" ".join(current))
        return chunks


