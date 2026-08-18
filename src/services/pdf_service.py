import PyPDF2
import io
from ..core.logger import logger
from . import llm_service


class PDFProcessingService:
    """Service for processing PDF files and generating summaries.

    Prompts, schemas and generation configs for the summary calls live in
    src/services/llm_service.py; this service owns PDF text extraction and document-type routing.
    """

    def extract_text_from_pdf(self, pdf_content: bytes) -> str:
        """
        Extract text content from PDF bytes

        Args:
            pdf_content: PDF file content as bytes

        Returns:
            str: Extracted text content from PDF
        """
        try:
            logger.info("Starting PDF text extraction")

            # Create a BytesIO object from the PDF content
            pdf_stream = io.BytesIO(pdf_content)

            # Create PDF reader object
            pdf_reader = PyPDF2.PdfReader(pdf_stream)

            # Extract text from all pages
            extracted_text = ""
            total_pages = len(pdf_reader.pages)

            logger.info(f"Processing PDF with {total_pages} pages")

            for page_num, page in enumerate(pdf_reader.pages):
                try:
                    page_text = page.extract_text()
                    extracted_text += f"\n--- Page {page_num + 1} ---\n{page_text}\n"
                    logger.debug(f"Extracted text from page {page_num + 1}")
                except Exception as e:
                    logger.warning(f"Failed to extract text from page {page_num + 1}: {str(e)}")
                    continue

            if not extracted_text.strip():
                logger.warning("No text content extracted from PDF")
                return ""

            logger.info(f"Successfully extracted {len(extracted_text)} characters from PDF")
            return extracted_text.strip()

        except Exception as e:
            logger.error(f"Error extracting text from PDF: {str(e)}")
            raise Exception(f"PDF text extraction failed: {str(e)}")

    async def generate_acbp_plan_summary(self, text_content: bytes) -> str:
        """Generate summary for ACBP Plan"""
        try:
            summary = await llm_service.summarize_acbp_plan(text_content)
            return summary or "Summary generation failed - no content returned"
        except Exception as e:
            logger.error(f"Error generating ACBP Plan summary: {str(e)}")
            return f"Summary generation failed: {str(e)}"

    async def generate_work_allocation_summary(self, text_content: bytes) -> str:
        """Generate summary for Work Allocation Order"""
        try:
            summary = await llm_service.summarize_work_allocation(text_content)
            return summary or "Summary generation failed - no content returned"
        except Exception as e:
            logger.error(f"Error generating Work Allocation Order summary: {str(e)}")
            return f"Summary generation failed: {str(e)}"

    async def process_pdf_and_generate_summary(self, pdf_content: bytes, document_type: str) -> str:
        """
        Process PDF and generate appropriate summary based on document type

        Args:
            pdf_content: PDF file content as bytes
            document_type: Type of document ('acbp_plan' or 'work_allocation')

        Returns:
            str: Generated summary
        """
        try:
            logger.info(f"Processing PDF for document type: {document_type}")

            # Generate summary based on document type
            if document_type == "acbp_plan":
                summary = await self.generate_acbp_plan_summary(pdf_content)
            elif document_type == "work_allocation":
                summary = await self.generate_work_allocation_summary(pdf_content)
            else:
                logger.error(f"Unknown document type: {document_type}")
                return f"Unknown document type: {document_type}"

            logger.info(f"Successfully processed PDF and generated summary for {document_type}")
            return summary

        except Exception as e:
            logger.error(f"Error processing PDF for {document_type}: {str(e)}")
            return f"PDF processing failed: {str(e)}"

# Create a singleton instance
pdf_service = PDFProcessingService()
