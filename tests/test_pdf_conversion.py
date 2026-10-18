"""Tests for chunked HTML-to-PDF conversion and PDF merging in src/utils/common.py."""
import asyncio
import io
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from PyPDF2 import PdfReader, PdfWriter

from src.core.configs import settings
from src.utils import common


def make_pdf(widths):
    """Build a PDF with one blank page per width, so page order can be checked by width."""
    writer = PdfWriter()
    for width in widths:
        writer.add_blank_page(width=width, height=200)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def page_widths(pdf_bytes):
    return [round(float(page.mediabox.width)) for page in PdfReader(io.BytesIO(pdf_bytes)).pages]


def chunk_html(index):
    return f"<html><body data-chunk='{index}'>chunk {index}</body></html>"


class FakePage:
    def __init__(self, browser):
        self.browser = browser
        self.html = None
        self.closed = False

    async def set_content(self, html, wait_until):
        self.html = html
        self.browser.wait_until_values.append(wait_until)

    async def pdf(self, **options):
        self.browser.pdf_options.append(options)
        # Yield so other chunks get scheduled and overlap with this one
        await asyncio.sleep(0.01)
        index = int(self.html.split("data-chunk='")[1].split("'")[0])
        if index in self.browser.failing_chunks:
            raise RuntimeError(f"print failed for chunk {index}")
        return make_pdf([100 + index])

    async def close(self):
        self.closed = True
        self.browser.open_pages -= 1


class FakeBrowser:
    def __init__(self, failing_chunks=(), close_error=None):
        self.failing_chunks = set(failing_chunks)
        self.close_error = close_error
        self.pages = []
        self.open_pages = 0
        self.max_open_pages = 0
        self.close_calls = 0
        self.wait_until_values = []
        self.pdf_options = []

    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        self.open_pages += 1
        self.max_open_pages = max(self.max_open_pages, self.open_pages)
        return page

    async def close(self):
        self.close_calls += 1
        if self.close_error:
            raise self.close_error


class FakeChromium:
    def __init__(self, browser, launch_error=None):
        self.browser = browser
        self.launch_error = launch_error
        self.launch_options = None

    async def launch(self, **options):
        self.launch_options = options
        if self.launch_error:
            raise self.launch_error
        return self.browser


class FakePlaywrightContext:
    def __init__(self, chromium):
        self.chromium = chromium

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def use_fake_playwright(browser, launch_error=None):
    chromium = FakeChromium(browser, launch_error)
    patcher = patch.object(common, "async_playwright", lambda: FakePlaywrightContext(chromium))
    return chromium, patcher


async def test_single_chunk_returns_printed_pdf_without_merging():
    browser = FakeBrowser()
    chromium, patcher = use_fake_playwright(browser)
    with patcher, patch.object(common, "merge_pdfs") as merge:
        pdf = await common.convert_html_chunks_to_pdf([chunk_html(0)])

    merge.assert_not_called()
    assert page_widths(pdf) == [100]
    assert chromium.launch_options == {"headless": True, "args": ["--no-sandbox"]}
    assert browser.wait_until_values == ["networkidle"]
    assert browser.pdf_options[0]["format"] == "A4"
    assert browser.pdf_options[0]["print_background"] is True
    assert browser.close_calls == 1


async def test_multiple_chunks_are_merged_in_input_order():
    browser = FakeBrowser()
    _, patcher = use_fake_playwright(browser)
    with patcher:
        pdf = await common.convert_html_chunks_to_pdf([chunk_html(i) for i in range(6)])

    assert page_widths(pdf) == [100, 101, 102, 103, 104, 105]
    assert all(page.closed for page in browser.pages)
    assert browser.close_calls == 1


async def test_concurrent_pages_are_limited_by_setting(monkeypatch):
    monkeypatch.setattr(settings, "REPORT_PDF_MAX_CONCURRENT_PAGES", 2)
    browser = FakeBrowser()
    _, patcher = use_fake_playwright(browser)
    with patcher:
        await common.convert_html_chunks_to_pdf([chunk_html(i) for i in range(7)])

    assert browser.max_open_pages == 2
    assert len(browser.pages) == 7


async def test_chunks_are_printed_in_parallel_up_to_the_limit(monkeypatch):
    monkeypatch.setattr(settings, "REPORT_PDF_MAX_CONCURRENT_PAGES", 4)
    browser = FakeBrowser()
    _, patcher = use_fake_playwright(browser)
    with patcher:
        await common.convert_html_chunks_to_pdf([chunk_html(i) for i in range(8)])

    assert browser.max_open_pages == 4


async def test_print_failure_raises_http_500_and_closes_every_page():
    browser = FakeBrowser(failing_chunks={2})
    _, patcher = use_fake_playwright(browser)
    with patcher, pytest.raises(HTTPException) as error:
        await common.convert_html_chunks_to_pdf([chunk_html(i) for i in range(4)])

    assert error.value.status_code == 500
    assert error.value.detail == "Error generating PDF with Playwright"
    assert all(page.closed for page in browser.pages)
    assert browser.close_calls == 1


async def test_launch_failure_raises_http_500_without_closing_a_browser():
    browser = FakeBrowser()
    _, patcher = use_fake_playwright(browser, launch_error=RuntimeError("chromium missing"))
    with patcher, pytest.raises(HTTPException) as error:
        await common.convert_html_chunks_to_pdf([chunk_html(0)])

    assert error.value.status_code == 500
    assert browser.close_calls == 0


async def test_browser_close_error_during_cleanup_keeps_the_original_error():
    browser = FakeBrowser(failing_chunks={0}, close_error=RuntimeError("close failed"))
    _, patcher = use_fake_playwright(browser)
    with patcher, pytest.raises(HTTPException) as error:
        await common.convert_html_chunks_to_pdf([chunk_html(0)])

    assert error.value.status_code == 500
    assert browser.close_calls == 1


def test_merge_pdfs_concatenates_all_pages_in_order():
    merged = common.merge_pdfs([make_pdf([10]), make_pdf([20, 30]), make_pdf([40])])

    assert page_widths(merged) == [10, 20, 30, 40]


def test_merge_pdfs_with_single_part_keeps_its_pages():
    assert page_widths(common.merge_pdfs([make_pdf([10, 20])])) == [10, 20]


@pytest.fixture(scope="module")
def chromium_available():
    """Skip real-browser tests where Playwright's Chromium is not installed (e.g. CI)."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            playwright.chromium.launch(headless=True, args=["--no-sandbox"]).close()
    except Exception as error:
        pytest.skip(f"Playwright Chromium not available: {error}")


@pytest.mark.usefixtures("chromium_available")
async def test_real_chromium_prints_and_merges_chunks():
    chunks = [f"<html><body><h1>Chunk {i}</h1></body></html>" for i in range(3)]

    pdf = await common.convert_html_chunks_to_pdf(chunks)

    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 3
    assert [f"Chunk {i}" in reader.pages[i].extract_text() for i in range(3)] == [True, True, True]
