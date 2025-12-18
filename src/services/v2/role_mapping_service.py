# Create a new file: src/role_mapping_service.py

import json
from typing import Dict, Any, List, Optional
from google import genai
from google.genai import types

from ...core.configs import settings
from ...prompts.v2.prompts import ROLE_MAPPING_PROMPT_V2, ROLE_MAPPING_PROMPT_V5_STATE
from ...crud.document import crud_document
from ...core.logger import logger

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
    """Service for generating role mappings using Google AI"""
    
    def __init__(self):
        """Initialize the role mapping service with Google AI configuration"""
        try:
            self.client = genai.Client(
                project=settings.GOOGLE_PROJECT_ID,
                location="us-central1",
                vertexai=True
            )
            logger.info("Google AI service for role mapping initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Google AI service for role mapping: {str(e)}")
            raise
    
    async def _call_gemini(
        self, 
        organization_data: Dict[str, Any],
        additional_document_contents: List[bytes] | None
    ) -> Dict[str, Any]:
        """
        Call Google Gemini to generate role mapping
        
        Args:
            organization_data: Dictionary containing ACBP and work allocation summaries

        Returns:
            Dict containing designations, role_responsibilities, activities, and competencies
        """
        try:
            logger.info(f"Generating role mapping for {organization_data.get('organization_name')}")
            output_json_format = [{
                "designation_name": "[Designation Name]",
                "wing_division_section": "[Wing/Division/Section]",
                "role_responsibilities": "[List of Role Responsibilities]",
                "activities": "[List of Activities]",
                "competencies": [
                    {
                        "type": "[Behavioral/Functional/Domain]",
                        "theme": "[Competency Theme]",
                        "sub_theme": "[Competency Sub-theme]",
                    }
                ],
                "source": "[ACBP, Work Allocation Order, KCM, AI Suggested]"
            },
            ]
            logger.info(f"Role Mapping is using prompt :: {'STATE_PROMPT' if organization_data["department_id"] else "CENTER_PROMPT"}")
            PROMPT = ROLE_MAPPING_PROMPT_V5_STATE if organization_data["department_id"] else ROLE_MAPPING_PROMPT_V2
            output_json_format = state_json_output if organization_data["department_id"] else center_json_output
            base_prompt = PROMPT.format(
                organization_name=organization_data.get('organization_name'),
                department_name=organization_data.get('department_name'),
                sector=organization_data.get('sector'),
                instructions=organization_data.get('instruction'),
                primary_summary=organization_data.get('docs_summary'),
                kcm_competencies=json.dumps(COMPETENCY_MAPPING, indent=2),
                output_json_format=json.dumps(output_json_format, indent=2)
            )
            
            
            contents = [
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=base_prompt)]
                )
            ]

            if additional_document_contents:
                for document_bytes in additional_document_contents:
                    pdf_part = types.Part.from_bytes(
                                data=document_bytes,
                                mime_type='application/pdf',
                            )
                    contents[0].parts.insert(0, pdf_part)
            
            # Configure the generation
            generate_content_config = types.GenerateContentConfig(
                temperature=0.5,
                # response_mime_type="application/json",
                # response_schema= {
                #     "type": "ARRAY",
                #     "items": {
                #         "type": "OBJECT",
                #         "properties": {
                #             "designation_name": {
                #                 "type": "STRING",
                #                 "description": "The official designation or job title for the role."
                #             },
                #             "wing_division_section": {
                #                 "type": "STRING",
                #                 "description": "The organizational unit (wing, division, or section) where the role is situated."
                #             },
                #             "role_responsibilities": {
                #                 "type": "ARRAY",
                #                 "items": {"type": "STRING"},
                #                 "description": "A list of 3–6 concise, action-oriented role responsibilities."
                #             },
                #             "activities": {
                #                 "type": "ARRAY",
                #                 "items": {"type": "STRING"},
                #                 "description": "A list of 4–8 activities or tasks aligned to the role responsibilities."
                #             },
                #             "competencies": {
                #                 "type": "ARRAY",
                #                 "items": {
                #                     "type": "OBJECT",
                #                     "properties": {
                #                         "type": {
                #                             "type": "STRING",
                #                             "enum": ["Behavioral", "Functional", "Domain"],
                #                             "description": "The category of competency as per Karmayogi framework."
                #                         },
                #                         "theme": {
                #                             "type": "STRING",
                #                             "description": "The parent theme of the competency (must come from dataset)."
                #                         },
                #                         "sub_theme": {
                #                             "type": "STRING",
                #                             "description": "The sub-theme of the competency (must come from dataset)."
                #                         }
                #                     },
                #                     "required": ["type", "theme", "sub_theme"]
                #                 },
                #                 "description": "A list of competencies relevant to the role. Must include at least one Behavioral, one Functional, and one Domain competency."
                #             }
                #         },
                #         "required": ["designation_name", "wing_division_section", "role_responsibilities", "activities", "competencies"]
                #     }
                # }   
            )
            
            # Generate content
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-pro",
                contents=contents,
                config=generate_content_config,
            )
            
            logger.info(f"Role Mapping Gemini usage metadata: {response.usage_metadata}")
            
            text_response = response.text

            if not text_response:
                logger.error("Gemini response was empty or not in text format")
                raise Exception("Empty response from Gemini")
            
            text_response = text_response.replace("```json", '')
            text_response = text_response.replace("```", '')
            parsed_response = json.loads(text_response)
            # logger.info(f"Successfully generated role mapping with {len(parsed_response.get('role_responsibilities', []))} responsibilities, {len(parsed_response.get('activities', []))} activities, and {len(parsed_response.get('competencies', []))} competencies")
            
            return parsed_response
            
        except Exception as e:
            logger.error(f"Error generating role mapping from Gemini: {str(e)}")
            raise Exception(f"Role mapping generation failed: {str(e)}")
    
    async def get_documents_summary(self, state_center_id, department_id = None) -> str:
        # Start with base query
        _, retrieved_docs = await crud_document.get_all_documents_async(state_center_id, department_id)
        if not retrieved_docs:
            return []
        
        docs_content = "\n\n".join(doc.summary_text for doc in retrieved_docs)
        return docs_content

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
            docs_summary = await self.get_documents_summary(state_center_id, department_id)
            
            # if not docs_summary:
            #     logger.warning(f"No document data found for ID: {state_center_id}")
            #     raise Exception("No document data found for this state/center")
            
            # Prepare organization data
            organization_data = {
                "state_center_id": state_center_id,
                "department_id" : department_id,
                "organization_name": state_center_name,
                "department_name": department_name if department_name else "N/A",
                "docs_summary": docs_summary if docs_summary else 'N/A',
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
