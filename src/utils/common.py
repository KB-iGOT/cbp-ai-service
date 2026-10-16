import asyncio
from datetime import datetime
import io
from typing import List
import uuid

from fastapi import HTTPException
from PyPDF2 import PdfWriter
from ..core.configs import settings
from ..core.logger import logger
from playwright.async_api import async_playwright

def convert_for_json(data_list):
    """
    Recursively convert UUIDs and datetime objects in a list of dicts to JSON-serializable types
    """
    for item in data_list:
        for k, v in item.items():
            if isinstance(v, uuid.UUID):
                item[k] = str(v)
            elif isinstance(v, datetime):
                item[k] = v.isoformat()
    return data_list


async def convert_html_chunks_to_pdf(html_chunks: List[str]) -> bytes:
    """
    Convert several HTML documents to one PDF using Playwright (Chromium headless).

    Each chunk is printed on its own page (up to REPORT_PDF_MAX_CONCURRENT_PAGES at once)
    in a single browser, and the PDFs are merged in order. Printing many small documents
    is much faster than one large one, because Chromium's print layout time grows
    superlinearly with document size.

    Args:
        html_chunks: HTML strings to convert, in output order
    Returns:
        PDF bytes
    """
    browser = None

    try:
        async with async_playwright() as p:
            # Launch options can be tuned for performance
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            semaphore = asyncio.Semaphore(settings.REPORT_PDF_MAX_CONCURRENT_PAGES)

            async def print_chunk(html_content: str) -> bytes:
                async with semaphore:
                    page = await browser.new_page()
                    try:
                        # OPTIMIZATION: Use set_content instead of writing to a temp file
                        # This keeps everything in memory and avoids disk I/O
                        await page.set_content(html_content, wait_until="networkidle")
                        return await page.pdf(
                            format="A4",
                            print_background=True,
                            margin={"top": "20px", "bottom": "20px", "left": "20px", "right": "20px"}
                        )
                    finally:
                        await page.close()

            pdf_parts = await asyncio.gather(*[print_chunk(html) for html in html_chunks])

            await browser.close()
            browser = None

        if len(pdf_parts) == 1:
            return pdf_parts[0]
        # Merging a large report is CPU-bound; keep it off the event loop
        return await asyncio.to_thread(merge_pdfs, pdf_parts)
    except Exception as e:
        logger.error(f"Error in Playwright PDF generation: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Error generating PDF with Playwright"
        )
    finally:
        # Ensure cleanup
        if browser:
            try:
                logger.info("Closing Playwright browser...")
                await browser.close()
            except Exception as e:
                logger.exception("Error closing browser")


def merge_pdfs(pdf_parts: List[bytes]) -> bytes:
    """Concatenate PDF documents in order into a single PDF."""
    writer = PdfWriter()
    for part in pdf_parts:
        writer.append(io.BytesIO(part))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
