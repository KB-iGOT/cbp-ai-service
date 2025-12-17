from datetime import datetime
import uuid

from fastapi import HTTPException
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


async def convert_html_to_pdf(html_content: str) -> bytes:
    """
    Convert HTML to PDF using Playwright (Chromium headless)

    Args:
        html_content: HTML string to convert
    Returns:
        PDF bytes
    """
    browser = None

    try:
        async with async_playwright() as p:
            # Launch options can be tuned for performance
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            
            # Open a new page
            page = await browser.new_page()
            
            # OPTIMIZATION: Use set_content instead of writing to a temp file
            # This keeps everything in memory and avoids disk I/O
            await page.set_content(html_content, wait_until="networkidle")

            pdf_bytes = await page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "20px", "bottom": "20px", "left": "20px", "right": "20px"}
            )

            await browser.close()
            return pdf_bytes
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
