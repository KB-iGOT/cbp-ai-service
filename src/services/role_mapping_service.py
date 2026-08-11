import json
from typing import Dict, Any, List, Optional

from ..crud.state_center_data import crud_state_center_data
from ..core.configs import settings
from ..prompts.prompts import ROLE_MAPPING_PROMPT_V2, ROLE_MAPPING_PROMPT_V5_STATE
from ..core.logger import logger
from .llm import GenerationConfig, Message, Part, get_llm

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
  "source": ["ACBP", "Work Allocation Order", "AI Suggested"]
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
  "source": ["Work Allocation Order" or "ACBP" or "Additional supporting document" or "AI Suggested"]
}]

class RoleMappingService:
    """Service for generating role mappings using an LLM"""

    def __init__(self):
        self.llm = get_llm()

    async def _call_gemini(
        self,
        organization_data: Dict[str, Any],
        additional_document_contents: List[bytes] | None
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

            logger.info(f"Role Mapping is using prompt :: {'STATE_PROMPT' if organization_data["department_id"] else "CENTER_PROMPT"}")
            PROMPT = ROLE_MAPPING_PROMPT_V5_STATE if organization_data["department_id"] else ROLE_MAPPING_PROMPT_V2
            output_json_format = state_json_output if organization_data["department_id"] else center_json_output
            base_prompt = PROMPT.format(
                organization_name=organization_data.get('organization_name'),
                department_name=organization_data.get('department_name'),
                sector=organization_data.get('sector'),
                instructions=organization_data.get('instruction'),
                acbp_summary=organization_data.get('acbp_plan_summary'),
                work_allocation_summary=organization_data.get('work_allocation_summary'),
                kcm_competencies=json.dumps(COMPETENCY_MAPPING, indent=2),
                output_json_format=json.dumps(output_json_format, indent=2)
            )

            items: List[str | Part] = []
            if additional_document_contents:
                for document_bytes in additional_document_contents:
                    items.append(Part.from_bytes(data=document_bytes, mime_type="application/pdf"))
            items.append(base_prompt)
            contents = [Message.user(*items)]

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
            logger.error(f"Error generating role mapping from LLM: {str(e)}")
            raise Exception(f"Role mapping generation failed: {str(e)}")

    # inside RoleMappingService
    async def _call_gemini_stream(
        self,
        organization_data: Dict[str, Any],
        additional_document: bytes | None
    ):
        """
        Stream role mapping generation from the LLM (yields chunks + final JSON)
        """
        try:
            PROMPT = ROLE_MAPPING_PROMPT_V5_STATE if organization_data["department_id"] else ROLE_MAPPING_PROMPT_V2
            output_json_format = state_json_output if organization_data["department_id"] else center_json_output
            base_prompt = PROMPT.format(
                organization_name=organization_data.get('organization_name'),
                department_name=organization_data.get('department_name'),
                sector=organization_data.get('sector'),
                instructions=organization_data.get('instruction'),
                acbp_summary=organization_data.get('acbp_plan_summary'),
                work_allocation_summary=organization_data.get('work_allocation_summary'),
                kcm_competencies=json.dumps(COMPETENCY_MAPPING, indent=2),
                output_json_format=json.dumps(output_json_format, indent=2)
            )

            items: List[str | Part] = []
            if additional_document:
                items.append(Part.from_bytes(data=additional_document, mime_type="application/pdf"))
            items.append(base_prompt)
            contents = [Message.user(*items)]

            buffer = []
            async for chunk in self.llm.stream(contents, model="gemini-2.5-pro", config=GenerationConfig(temperature=0.5)):
                buffer.append(chunk)
                yield {"type": "chunk", "data": chunk}

            # After stream finishes, parse JSON
            final_text = "".join(buffer).replace("```json", "").replace("```", "")
            parsed = json.loads(final_text)

            yield {"type": "final", "data": parsed}

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
            db (Session): SQLAlchemy database session.
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

            # if not state_center_data:
            #     logger.warning(f"No state center data found for ID: {state_center_id}")
            #     raise Exception("No ACBP plan or work allocation data found for this state/center")

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

            # Generate role mapping using thread pool for blocking call
            result = await self._call_gemini(organization_data, additional_document_contents)

            logger.info("Role mapping generation completed successfully")
            return result

        except Exception as e:
            logger.error(f"Error in role mapping generation: {str(e)}")
            raise

# Create a singleton instance
role_mapping_service = RoleMappingService()
