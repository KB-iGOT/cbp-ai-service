import json
from typing import Dict, Any, List, Optional
import uuid

from ...schemas.role_mapping import OrgType

from ...core.configs import settings
from ...prompts.v2.prompts import ROLE_MAPPING_PROMPT_V2, ROLE_MAPPING_PROMPT_V5_STATE
from ...crud.document import crud_document
from ...core.logger import logger
from ..llm import GenerationConfig, Message, get_llm

with open("data/competencies.json") as f:
    COMPETENCY_MAPPING = json.load(f)

center_json_output = [{
  "designation_name": "string",
  "wing_division_section": "string",
  "role_responsibilities": ["string", "string"],
  "activities": ["string", "string"],
  "sort_order": "integer",
  "competencies": [
    {
      "type": "Behavioral | Functional | Domain",
      "theme": "string",
      "sub_theme": "string",
      "source": "KCM or AI Suggested"
   }
  ],
  "source": ["Primary document summaries", "AI Suggested"]
}]

state_json_output = [{
  "designation_name": "string",
  "wing_division_section": "string",
  "role_responsibilities": ["string", "string"],
  "activities": ["string", "string"],
  "sort_order": "integer",
  "competencies": [
    {
      "type": "Behavioral | Functional | Domain",
      "theme": "string",
      "sub_theme": "string",
      "source": "KCM or AI Suggested"
    }
  ],
  "source": ["Primary document summaries", "AI Suggested"]
}]

class RoleMappingService:
    """Service for generating role mappings using an LLM"""

    def __init__(self):
        self.llm = get_llm()

    async def _call_gemini(
        self,
        organization_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Generate role mapping via the configured LLM

        Args:
            organization_data: Dictionary containing ACBP and work allocation summaries

        Returns:
            Dict containing designations, role_responsibilities, activities, and competencies
        """
        try:
            logger.info(f"Generating role mapping for {organization_data.get('organization_name')}")

            logger.info(f"Role Mapping is using prompt :: {'STATE_PROMPT' if organization_data["org_type"] == OrgType.state.value else "CENTER_PROMPT"}")
            PROMPT = ROLE_MAPPING_PROMPT_V5_STATE if organization_data["org_type"] == OrgType.state.value else ROLE_MAPPING_PROMPT_V2
            output_json_format = state_json_output if organization_data["org_type"] == OrgType.state.value else center_json_output
            base_prompt = PROMPT.format(
                organization_name=organization_data.get('organization_name'),
                department_name=organization_data.get('department_name'),
                instructions=organization_data.get('instruction'),
                primary_summary=organization_data.get('docs_summary'),
                kcm_competencies=json.dumps(COMPETENCY_MAPPING, indent=2),
                output_json_format=json.dumps(output_json_format, indent=2)
            )

            contents = [Message.user(base_prompt)]

            generate_content_config = GenerationConfig(temperature=0.5)

            response = await self.llm.generate(
                contents,
                model="gemini-2.5-pro",
                config=generate_content_config,
            )

            text_response = response.text

            if not text_response:
                logger.error("LLM response was empty or not in text format")
                raise Exception("Empty response from LLM")

            text_response = text_response.replace("```json", '')
            text_response = text_response.replace("```", '')
            parsed_response = json.loads(text_response)

            return parsed_response

        except Exception as e:
            logger.exception(f"Error generating role mapping from LLM")
            raise e

    async def get_documents_summary(self, user_id, state_center_id, department_id = None) -> str:
        # Start with base query
        _, retrieved_docs = await crud_document.get_all_documents_async(user_id, state_center_id, department_id)
        if not retrieved_docs:
            return []

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
            db (Session): SQLAlchemy database session.
            department_id (optional): ID of associated department. Defaults to None.
            department_name (optional): The name of associated department. Defaults to None.
            instruction (Optional[str], optional): Additional instructions. Defaults to None.


        Returns:
            Dictionary containing generated role mapping data
        """
        try:

            logger.info(f"Starting role mapping generation for state_center_id: {state_center_id}")

            # Fetch state center data
            docs_summary = await self.get_documents_summary(user_id,state_center_id, department_id)

            # if not docs_summary:
            #     logger.warning(f"No document data found for ID: {state_center_id}")
            #     raise Exception("No document data found for this state/center")

            # Prepare organization data
            organization_data = {
                "org_type": org_type.value,
                "state_center_id": state_center_id,
                "department_id" : department_id,
                "organization_name": state_center_name,
                "department_name": department_name if department_name else "N/A",
                "docs_summary": docs_summary if docs_summary else 'N/A',
                "instruction": instruction if instruction else "N/A"
            }

            # Generate role mapping using thread pool for blocking call
            result = await self._call_gemini(organization_data)

            logger.info("Role mapping generation completed successfully")
            return result

        except Exception as e:
            logger.exception(f"Error in role mapping generation:")
            raise

# Create a singleton instance
role_mapping_service = RoleMappingService()
