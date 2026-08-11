import PyPDF2
import io
from ..core.configs import settings
from ..prompts.prompts import ACBP_DOCUMENT_SUMMARY_PROMPT
from ..core.logger import logger
from .llm import GenerationConfig, Message, Part, SafetyPolicy, get_llm

WORK_ALLOCATION_SUMMARY_PROMPT = """
You are an expert analyst specializing in Work Allocation Order documents for government and organizational projects. Please analyze the provided Work Allocation Order PDF document and create a comprehensive, structured summary.

Focus on extracting and summarizing:
1. Work order details and reference numbers
2. Allocated tasks and responsibilities
3. Personnel assignments and roles
4. Timeline and deadlines
5. Resource allocation and budget
6. Deliverables and expected outcomes
7. Quality standards and requirements
8. Reporting and monitoring mechanisms
9. Approval authorities and stakeholders

**Output Format:**
Provide a well-structured summary that captures all essential elements of the Work Allocation Order. Use clear headings, bullet points where appropriate, and ensure the summary provides actionable insights for project execution.

Please analyze the PDF document and provide your comprehensive summary:
"""


class PDFProcessingService:
    """Service for processing PDF files and generating summaries using an LLM"""

    def __init__(self):
        self.llm = get_llm()

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

    async def _generate_pdf_summary(self, pdf_content: bytes, prompt: str, log_label: str) -> str:
        """Shared PDF-to-summary call used by both ACBP and Work Allocation summaries."""
        try:
            contents = [Message.user(
                Part.from_bytes(data=pdf_content, mime_type="application/pdf"),
                prompt,
            )]
            config = GenerationConfig(
                temperature=1,
                top_p=0.95,
                seed=0,
                max_output_tokens=65535,
                safety=SafetyPolicy.PERMISSIVE,
                thinking_budget=-1,
            )
            response = await self.llm.generate(contents, model="gemini-2.5-pro", config=config)

            summary = response.text
            if not summary:
                logger.warning(f"Empty {log_label} summary generated")
                return "Summary generation failed - no content returned"

            logger.info(f"Successfully generated {log_label} summary ({len(summary)} characters)")
            return summary

        except Exception as e:
            logger.error(f"Error generating {log_label} summary: {str(e)}")
            return f"Summary generation failed: {str(e)}"

    async def generate_acbp_plan_summary(self, text_content: bytes) -> str:
        """Generate summary for ACBP Plan using the configured LLM"""
        logger.info("Generating ACBP Plan summary")
        return await self._generate_pdf_summary(text_content, ACBP_DOCUMENT_SUMMARY_PROMPT, "ACBP Plan")

    async def generate_work_allocation_summary(self, text_content: bytes) -> str:
        """Generate summary for Work Allocation Order using the configured LLM"""
        logger.info("Generating Work Allocation Order summary")
        return await self._generate_pdf_summary(text_content, WORK_ALLOCATION_SUMMARY_PROMPT, "Work Allocation Order")

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
