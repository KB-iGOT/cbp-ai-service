# src/role_mapping_service.py
import json
from typing import Dict, Any, List, Optional
import uuid
import asyncio
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from ...schemas.role_mapping import OrgType
from ...core.configs import settings
from ...prompts.v3.prompts import (
    DESIGNATION_EXTRACTION_PROMPT,
    ROLE_MAPPING_PROMPT_CENTRE_V3, 
    ROLE_MAPPING_PROMPT_STATE_V3
)
from ...crud.document import crud_document
from ...core.logger import logger

with open("data/competencies.json") as f:
    COMPETENCY_MAPPING = json.load(f)

# src/prompts/v2/prompts.py (add this to your existing prompts file)


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
    "source": ["Work Allocation Order", "ACBP", "Additional supporting document", "AI Suggested"]
}]

class Designation(BaseModel):
    sort_order: int = Field(
        description="Hierarchical position, starting from 1 (highest) and incrementing sequentially"
    )
    designation: str = Field(
        description="The official designation or job title"
    )

class DesignationExtractionResponse(BaseModel):
    designations: List[Designation] = Field(
        description="Complete list of all extracted designations sorted by hierarchy"
    )

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
    
    async def _extract_designations(
        self,
        organization_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        PASS 1: Extract all designations from documents
        
        Args:
            organization_data: Dictionary containing document summaries
            additional_document_contents: Additional PDF documents
            
        Returns:
            Dict containing extracted designations with metadata
        """
        try:
            logger.info(f"PASS 1: Extracting designations for {organization_data.get('organization_name')}")
            
            extraction_output_format = {
                "designations": [
                    {
                        "sort_order": "integer",
                        "designation": "string"
                    }
                ]
            }
            
            base_prompt = DESIGNATION_EXTRACTION_PROMPT.format(
                primary_summary=organization_data.get('docs_summary'),
                organization_name=organization_data.get('organization_name'),
                department_name=organization_data.get('department_name'),
                output_format=json.dumps(extraction_output_format, indent=2)
            )
            
            contents = [
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=base_prompt)]
                )
            ]
            
            generate_content_config = types.GenerateContentConfig(
                temperature=0.3,  # Lower temperature for extraction accuracy
                response_mime_type="application/json",
                response_schema=DesignationExtractionResponse.model_json_schema()
            )
            
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=contents,
                config=generate_content_config,
            )
            
            logger.info(f"Designation Extraction Gemini usage: {response.usage_metadata}")
            
            text_response = response.text
            if not text_response:
                logger.error("Designation extraction response was empty")
                raise Exception("Empty response from Gemini during designation extraction")
            
            extraction_response = DesignationExtractionResponse.model_validate_json(text_response)
            return {
                "designations": [d.model_dump() for d in extraction_response.designations]
            }
            
        except Exception as e:
            logger.exception(f"Error in designation extraction")
            raise Exception(f"Designation extraction failed")
    
    async def _generate_frac_for_batch(
        self,
        designations_batch: List[Dict[str, Any]],
        organization_data: Dict[str, Any],
        batch_number: int
    ) -> List[Dict[str, Any]]:
        """
        PASS 2: Generate FRAC mapping for a batch of designations
        
        Args:
            designations_batch: List of designations to process
            organization_data: Organization context data
            additional_document_contents: Additional documents
            batch_number: Current batch number for logging
            
        Returns:
            List of FRAC mappings for the batch (empty list on failure)
        """
        try:
            logger.info(f"PASS 2 - Batch {batch_number}: Processing {len(designations_batch)} designations")
            
            logger.info(f"Role Mapping is using prompt :: {'STATE_PROMPT' if organization_data["org_type"] == OrgType.state.value else "CENTER_PROMPT"}")
            PROMPT = ROLE_MAPPING_PROMPT_STATE_V3 if organization_data["org_type"] == OrgType.state.value else ROLE_MAPPING_PROMPT_CENTRE_V3
            output_json_format = state_json_output if organization_data["org_type"] == OrgType.state.value else center_json_output
            
            # Create designation context for the batch
            designation_context = json.dumps({
                "validated_designations": designations_batch,
                "batch_info": {
                    "batch_number": batch_number,
                    "total_in_batch": len(designations_batch)
                }
            }, indent=2)
            
            base_prompt = PROMPT.format(
                pass1_output=designation_context,
                organization_name=organization_data.get('organization_name'),
                department_name=organization_data.get('department_name'),
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
            
            generate_content_config = types.GenerateContentConfig(
                temperature=0.5,
            )
            
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-pro",
                contents=contents,
                config=generate_content_config,
            )
            
            logger.info(f"FRAC Batch {batch_number} Gemini usage: {response.usage_metadata}")
            
            text_response = response.text
            if not text_response:
                logger.warning(f"Batch {batch_number}: Empty response, returning empty array")
                return []
            
            text_response = text_response.replace("```json", '').replace("```", '')
            parsed_response = json.loads(text_response)
            
            logger.info(f"Batch {batch_number}: Successfully generated {len(parsed_response)} FRAC mappings")
            return parsed_response
            
        except Exception as e:
            logger.error(f"Error in FRAC generation for batch {batch_number}: {str(e)}", exc_info=True)
            logger.warning(f"Batch {batch_number}: Returning empty array due to failure")
            return []  # Return empty array on failure, don't skip
    
    async def _process_batches_parallel(
        self,
        all_designations: List[Dict[str, Any]],
        organization_data: Dict[str, Any],
        batch_size: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Process designation batches in parallel
        
        Args:
            all_designations: All extracted designations
            organization_data: Organization context
            batch_size: Number of designations per batch
            
        Returns:
            Combined list of all FRAC mappings
        """
        # Split into batches
        batches = [
            all_designations[i:i + batch_size] 
            for i in range(0, len(all_designations), batch_size)
        ]
        
        logger.info(f"Processing {len(all_designations)} designations in {len(batches)} batches of {batch_size}")
        
        # Create tasks for parallel processing
        tasks = [
            self._generate_frac_for_batch(
                batch,
                organization_data,
                batch_number=idx + 1
            )
            for idx, batch in enumerate(batches)
        ]
        
        # Execute all batches in parallel
        batch_results = await asyncio.gather(*tasks, return_exceptions=False)
        
        # Combine all results (empty arrays are handled gracefully)
        combined_results = []
        for batch_num, result in enumerate(batch_results, 1):
            if isinstance(result, list):
                combined_results.extend(result)
                logger.info(f"Batch {batch_num}: Added {len(result)} mappings to final result")
            else:
                logger.warning(f"Batch {batch_num}: Unexpected result type, skipping")
        
        logger.info(f"Total FRAC mappings generated: {len(combined_results)}")
        return combined_results
    
    async def get_documents_summary(self, user_id, state_center_id, department_id=None) -> str:
        """Get document summaries for the organization"""
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
        Generate role mapping with two-pass approach:
        PASS 1: Extract all designations
        PASS 2: Generate FRAC mappings in batches
        
        Args:
            user_id: User ID
            state_center_id: ID of associated state/center instance
            state_center_name: Name of associated state/center
            department_name: Department name (optional)
            department_id: Department ID (optional)
            instruction: Additional instructions (optional)
            
        Returns:
            Dictionary containing:
                - designations_extracted: List of extracted designations
        """
        try:
            logger.info(f"Starting TWO-PASS role mapping for state_center_id: {state_center_id}")
            
            # Fetch document summaries
            docs_summary = await self.get_documents_summary(user_id, state_center_id, department_id)
            
            # Prepare organization data
            organization_data = {
                "org_type": org_type.value,
                "state_center_id": state_center_id,
                "department_id": department_id,
                "organization_name": state_center_name,
                "department_name": department_name if department_name else "N/A",
                "docs_summary": docs_summary if docs_summary else 'N/A',
                "instruction": instruction if instruction else "N/A"
            }
            
            # ============ PASS 1: DESIGNATION EXTRACTION ============
            logger.info("STARTING PASS 1: DESIGNATION EXTRACTION")
            
            extraction_result = await self._extract_designations(
                organization_data
            )
            print(extraction_result)
            designations = extraction_result.get('designations', [])
            if not designations:
                logger.warning("No designations extracted in PASS 1")
                return []
 
            logger.info(f"PASS 1 SUCCESS: {len(designations)} designations extracted")

            # ============ PASS 2: FRAC GENERATION IN BATCHES ============
            logger.info("STARTING PASS 2: FRAC GENERATION")
            
            frac_mappings = await self._process_batches_parallel(
                designations,
                organization_data,
                batch_size=30
            )

            logger.info("TWO-PASS ROLE MAPPING COMPLETE")
            logger.info(f"Designations Extracted: {len(designations)}")
            logger.info(f"FRAC Mappings Generated: {len(frac_mappings)}")

            return frac_mappings
        except Exception as e:
            logger.exception(f"Error in two-pass role mapping generation:")
            raise

# Create a singleton instance
role_mapping_service = RoleMappingService()