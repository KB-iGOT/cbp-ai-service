from typing import Dict, Any, List, Optional

from ..crud.state_center_data import crud_state_center_data
from ..core.logger import logger
from . import llm_service


class RoleMappingService:
    """v1 role mapping.

    Prompts, schemas and generation configs live in src/services/llm_service.py; this service
    owns data fetching and orchestration.
    """

    async def _call_gemini(
        self,
        organization_data: Dict[str, Any],
        additional_document_contents: List[bytes] | None
    ) -> Dict[str, Any]:
        """Generate role mapping via the configured LLM."""
        try:
            return await llm_service.generate_role_mapping_v1(
                organization_data,
                additional_document_contents=additional_document_contents,
            )
        except Exception as e:
            logger.error(f"Error generating role mapping from LLM: {str(e)}")
            raise Exception(f"Role mapping generation failed: {str(e)}")

    async def _call_gemini_stream(
        self,
        organization_data: Dict[str, Any],
        additional_document: bytes | None
    ):
        """Stream role mapping generation (yields chunks + final JSON)."""
        try:
            async for event in llm_service.stream_role_mapping_v1(
                organization_data,
                additional_document=additional_document,
            ):
                yield event
        except Exception as e:
            logger.error(f"Error streaming role mapping from LLM: {str(e)}")
            raise Exception(f"Role mapping streaming failed: {str(e)}")

    async def generate_role_mapping(
        self,
        state_center_id: str,
        state_center_name: str,
        additional_document_contents: List[bytes] | None,
        department_name: Optional[str] = None,
        department_id: Optional[str] = None,
        sector: Optional[str] = None,
        instruction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate role mapping asynchronously

        Args:
            state_center_id : ID of associated state/center instance.
            state_center_name:  Name of associated state/center
            department_id (optional): ID of associated department. Defaults to None.
            department_name (optional): The name of associated department. Defaults to None.
            sector (Optional[str], optional): The name of the sector. Defaults to None.
            instruction (Optional[str], optional): Additional instructions. Defaults to None.

        Returns:
            Dictionary containing generated role mapping data
        """
        try:
            logger.info(f"Starting role mapping generation for state_center_id: {state_center_id}")

            # Fetch state center data
            state_center_data = await crud_state_center_data.get_by_state_center_and_department(state_center_id, department_id)

            # Prepare organization data
            organization_data = {
                "state_center_id": state_center_id,
                "department_id" : department_id,
                "organization_name": state_center_name,
                "department_name": department_name if department_name else "N/A",
                "acbp_plan_summary": state_center_data.acbp_plan_summary if state_center_data else 'N/A',
                "work_allocation_summary": state_center_data.work_allocation_order_summary if state_center_data else 'N/A',
                "sector": sector if sector else "N/A",
                "instruction": instruction if instruction else "N/A"
            }

            result = await self._call_gemini(organization_data, additional_document_contents)

            logger.info("Role mapping generation completed successfully")
            return result

        except Exception as e:
            logger.error(f"Error in role mapping generation: {str(e)}")
            raise

# Create a singleton instance
role_mapping_service = RoleMappingService()
