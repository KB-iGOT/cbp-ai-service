from typing import Dict, Any, Optional
import uuid

from ...schemas.role_mapping import OrgType
from ...crud.document import crud_document
from ...core.logger import logger
from .. import llm_service

class RoleMappingService:
    """v2 role mapping.

    Prompts, schemas and generation configs live in src/services/llm_service.py; this service
    owns data fetching and orchestration.
    """

    async def _call_gemini(self, organization_data: Dict[str, Any]) -> Dict[str, Any]:
        """Generate role mapping via the configured LLM."""
        try:
            return await llm_service.generate_role_mapping_v2(organization_data)
        except Exception as e:
            logger.exception("Error generating role mapping from LLM")
            raise e

    async def get_documents_summary(self, user_id, state_center_id, department_id = None) -> str:
        # Start with base query
        _, retrieved_docs = await crud_document.get_all_documents_async(user_id, state_center_id, department_id)
        if not retrieved_docs:
            return ""

        docs_content = "\n\n".join(doc.summary_text for doc in retrieved_docs)
        return docs_content

    async def generate_role_mapping(
        self,
        user_id: uuid.UUID,
        org_type: OrgType,
        state_center_id: str,
        state_center_name: str,
        department_name: Optional[str] = None,
        department_id: Optional[str] = None,
        instruction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate role mapping asynchronously

        Args:
            state_center_id : ID of associated state/center instance.
            state_center_name:  Name of associated state/center
            department_id (optional): ID of associated department. Defaults to None.
            department_name (optional): The name of associated department. Defaults to None.
            instruction (Optional[str], optional): Additional instructions. Defaults to None.

        Returns:
            Dictionary containing generated role mapping data
        """
        try:
            logger.info(f"Starting role mapping generation for state_center_id: {state_center_id}")

            docs_summary = await self.get_documents_summary(user_id, state_center_id, department_id)

            organization_data = {
                "org_type": org_type.value,
                "state_center_id": state_center_id,
                "department_id" : department_id,
                "organization_name": state_center_name,
                "department_name": department_name if department_name else "N/A",
                "docs_summary": docs_summary if docs_summary else 'N/A',
                "instruction": instruction if instruction else "N/A"
            }

            result = await self._call_gemini(organization_data)

            logger.info("Role mapping generation completed successfully")
            return result

        except Exception as e:
            logger.exception("Error in role mapping generation:")
            raise

# Create a singleton instance
role_mapping_service = RoleMappingService()
