# src/role_mapping_service.py
import json
from typing import Dict, Any, List, Literal, Optional
import uuid
import asyncio
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from ...schemas.role_mapping import OrgType
from ...core.configs import settings
from ...prompts.v3.prompts import (
    DESIGNATION_EXTRACTION_PROMPT,
    ROLE_MAPPING_PROMPT_CENTRE,
    ROLE_MAPPING_PROMPT_STATE,
    DOMAIN_FROM_WAO_PROMPT
)
from ...crud.document import crud_document
from ...services.storage_service import get_storage_service
from ...core.logger import logger

with open("data/competencies.json") as f:
    COMPETENCY_MAPPING = json.load(f)

# Response schema for the per-designation domain-from-WAO pass (PASS 3)
DOMAIN_FROM_WAO_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"theme": {"type": "STRING"}, "sub_theme": {"type": "STRING"}},
        "required": ["theme", "sub_theme"],
    },
}

# src/prompts/v2/prompts.py (add this to your existing prompts file)


center_json_output = {
    "mappings": [{
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
}

state_json_output = {
    "mappings": [{
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
}

class Designation(BaseModel):
    sort_order: int = Field(
        description="Hierarchical position, starting from 1 (highest) and incrementing sequentially"
    )
    designation: str = Field(
        description="Exact official designation or job title"
    )
    wing_division_section: str = Field(
        description="Exact wing/division/section the designation belongs to, or org unit / administrative from source document"
    )

class DesignationExtractionResponse(BaseModel):
    designations: List[Designation] = Field(
        description="List of extracted unique designations sorted by hierarchy"
    )


class FRACCompetency(BaseModel):
    type: Literal["Behavioral", "Functional", "Domain"] = Field(description="Competency type: Behavioral, Functional, or Domain")
    theme: str = Field(description="Competency theme")
    sub_theme: str = Field(description="Competency sub theme")
    source: Optional[str] = Field(default=None, description="Competency source")


class FRACRoleMapping(BaseModel):
    designation_name: str = Field(description="Official designation name")
    wing_division_section: str = Field(description="Wing/division/section the designation belongs to")
    role_responsibilities: List[str] = Field(description="Flat list of role responsibilities as strings")
    activities: List[str] = Field(description="Flat list of activity strings")
    sort_order: int = Field(description="Hierarchy sort order, strictly increasing from 1")
    competencies: List[FRACCompetency] = Field(description="Flat list of competency objects")
    source: Optional[List[str]] = Field(default=None, description="Source references")


class FRACBatchResponse(BaseModel):
    mappings: List[FRACRoleMapping] = Field(description="List of FRAC role mappings for all designations in the batch")

class RoleMappingService:
    """Service for generating role mappings using Google AI"""
    
    def __init__(self):
        """Initialize the role mapping service with Google AI configuration"""
        try:
            self.client = genai.Client(
                project=settings.GOOGLE_PROJECT_ID,
                location="global",
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
                    
            contents = [
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(
                        text="""
                        Here is the input Data:
                        Ministry/State Name: {ORGANIZATION_NAME}
                        Department/Organisation Name: {DEPARTMENT_NAME}

                        Primary reference document Summaries:
                        {DOCUMENT_SUMMARIES}

                        Extract ALL unique designations from the provided input data and organize them hierarchically based on the system prompt.
                        """.format(
                                ORGANIZATION_NAME=organization_data.get('organization_name'),
                                DEPARTMENT_NAME=organization_data.get('department_name'),
                                DOCUMENT_SUMMARIES=organization_data.get('docs_summary', 'N/A')
                            )
                    )]
                )
            ]
            
            generate_content_config = types.GenerateContentConfig(
                system_instruction=DESIGNATION_EXTRACTION_PROMPT,
                temperature=0.1,   # Very low — factual extraction, no creativity
                top_p=0.85, # Restrict to high-probability tokens
                response_mime_type="application/json",
                response_schema=DesignationExtractionResponse.model_json_schema()
            )
            
            response = await self.client.aio.models.generate_content(
                model="gemini-3.5-flash",
                contents=contents,
                config=generate_content_config,
            )
            print(response.candidates)
            text_response = response.text
            if not text_response:
                logger.error("Designation extraction response was empty")
                raise Exception("Empty response from Gemini during designation extraction")
            
            extraction_response = DesignationExtractionResponse.model_validate_json(text_response)
            return {
                "designations": [d.model_dump() for d in extraction_response.designations]
            }
            
        except Exception as e:
            logger.exception("Error in designation extraction")  
            raise Exception("Designation extraction failed") from e  # Chain exception
    
    async def _generate_frac_for_batch(
        self,
        designations_batch: List[Dict[str, Any]],
        organization_data: Dict[str, Any],
        batch_number: int,
        max_retries: int = 2
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
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"PASS 2 - Batch {batch_number} (attempt {attempt}/{max_retries}): Processing {len(designations_batch)} designations")
                logger.info(f"Role Mapping is using prompt :: {'STATE_PROMPT' if organization_data["org_type"] == OrgType.state.value else "CENTER_PROMPT"}")
                PROMPT = ROLE_MAPPING_PROMPT_STATE if organization_data["org_type"] == OrgType.state.value else ROLE_MAPPING_PROMPT_CENTRE
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
                    temperature=0.3,
                    top_p=0.90,
                    response_mime_type="application/json",
                    response_schema=FRACBatchResponse.model_json_schema(),
                )

                response = await self.client.aio.models.generate_content(
                    model="gemini-3.1-pro-preview",
                    contents=contents,
                    config=generate_content_config,
                )

                logger.info(f"FRAC Batch {batch_number} Gemini usage: {response.usage_metadata}")

                text_response = response.text
                if not text_response:
                    logger.warning(f"Batch {batch_number}: Empty response on attempt {attempt}")
                    if attempt < max_retries:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return []

                batch_response = FRACBatchResponse.model_validate_json(text_response)
                validated_response = [record.model_dump() for record in batch_response.mappings]

                logger.info(f"Batch {batch_number}: Successfully generated {len(validated_response)} FRAC mappings")
                return validated_response
            except json.JSONDecodeError as e:
                logger.warning(f"Batch {batch_number}: JSON parse error on attempt {attempt}: {str(e)}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return [] 
            except Exception as e:
                logger.error(f"Batch {batch_number}: Error on attempt {attempt}: {str(e)}", exc_info=True)
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return []
        return []
    
    async def _process_batches_parallel(
        self,
        all_designations: List[Dict[str, Any]],
        organization_data: Dict[str, Any],
        batch_size: int = 50,
        # max_concurrent: int = 3
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
        
        # Too Many Parallel Batches Can Hit Rate Limits
        # semaphore = asyncio.Semaphore(max_concurrent)
        # async def limited_batch(batch, idx):
        #     async with semaphore:
        #         return await self._generate_frac_for_batch(
        #             batch, organization_data, batch_number=idx + 1
        #         )
        # tasks = [limited_batch(batch, idx) for idx, batch in enumerate(batches)]
        
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
    
    async def get_documents_summary(self, user_id, state_center_id, department_id=None, document_type: str | None = None) -> str:
        """Get document summaries for the organization formatted as numbered document_summary tags
        
        Args:
            user_id: User ID
            state_center_id: State center ID
            department_id: Optional department ID
            document_type: Optional filter for document type (e.g., 'Work Allocation Order')
            
        Returns:
            Formatted document summaries
        """
        _, retrieved_docs = await crud_document.get_all_documents_async(user_id, state_center_id, department_id, document_type=document_type)
        if not retrieved_docs:
            return ""
        
        parts = []
        for idx, doc in enumerate(retrieved_docs, start=1):
            summary = (doc.summary_text or "").strip()
            parts.append(f"<document_summary_{idx}>\n   {summary}\n</document_summary_{idx}>")
        
        return "\n\n".join(parts)
    
    async def _get_wao_pdf_parts(
        self, user_id, state_center_id, department_id=None,
        document_type: str | None = "Work Allocation Order"
    ) -> List[types.Part]:
        """Read the WAO document(s) from storage as Gemini PDF Parts.

        The PDF bytes are sent to Gemini directly (native PDF understanding), so scanned/image
        pages, tables and column layouts are handled — mirroring how the summary is generated
        (Part.from_bytes(application/pdf)). Returns [] on any failure so PASS 3 falls back to the
        summary-based domain competencies (non-breaking).
        """
        try:
            _, docs = await crud_document.get_all_documents_async(
                user_id, state_center_id, department_id, document_type=document_type)
            if not docs:
                return []
            storage = get_storage_service()
            parts = []
            for doc in docs:
                try:
                    pdf_bytes = storage.read_file(doc.stored_path)
                    if pdf_bytes:
                        parts.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
                except Exception as e:
                    logger.warning(f"WAO PDF read failed for '{getattr(doc, 'stored_path', '?')}': {e}")
            return parts
        except Exception as e:
            logger.warning(f"_get_wao_pdf_parts failed: {e}")
            return []

    async def _create_wao_cache(self, pdf_parts: List[types.Part]) -> Optional[str]:
        """Create a Gemini context cache holding the WAO PDF(s), reused across the per-designation
        domain calls (the PDF is uploaded/charged once, not per call). Returns the cache name, or
        None if caching is disabled (DOMAIN_FROM_WAO_CACHE_TTL_SECONDS <= 0) or unavailable (e.g.
        the doc is below the model's minimum cacheable size) — the caller then sends the PDF inline
        on each call (still correct, just less token-efficient)."""
        ttl = settings.DOMAIN_FROM_WAO_CACHE_TTL_SECONDS
        if not ttl or ttl <= 0:
            logger.info("WAO context caching disabled (DOMAIN_FROM_WAO_CACHE_TTL_SECONDS<=0); sending PDF inline per call")
            return None
        try:
            cache = await self.client.aio.caches.create(
                model="gemini-3.1-pro-preview",
                config=types.CreateCachedContentConfig(
                    display_name="wao-domain",
                    contents=[types.Content(role="user", parts=pdf_parts)],
                    ttl=f"{ttl}s"))
            logger.info(f"WAO context cache created ({cache.name}, ttl={ttl}s) — reused across designations")
            return cache.name
        except Exception as e:
            logger.warning(f"WAO context cache unavailable ({e}); sending PDF inline per call")
            return None

    async def _delete_wao_cache(self, cache_name: str):
        """Best-effort cache cleanup (it would otherwise expire via TTL)."""
        try:
            await self.client.aio.caches.delete(name=cache_name)
        except Exception as e:
            logger.warning(f"WAO cache delete failed for {cache_name}: {e} (will expire via TTL)")

    async def _generate_domain_from_wao(
        self, organization_data: Dict[str, Any], designation: str, wing: str,
        pdf_parts: List[types.Part], cache_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """PASS 3 (per designation): derive Domain competencies from the WAO PDF. Uses the shared
        context cache when available, else sends the PDF inline. Returns a list of
        {type:'Domain', theme, sub_theme, source} (empty on failure)."""
        prompt = DOMAIN_FROM_WAO_PROMPT.format(
            organization_name=organization_data.get("organization_name"),
            department_name=organization_data.get("department_name"),
            designation=designation,
            wing=wing or "N/A",
        )
        gen_kwargs = dict(
            temperature=0.3, top_p=0.90, max_output_tokens=32768,
            response_mime_type="application/json", response_schema=DOMAIN_FROM_WAO_SCHEMA)
        if cache_name:
            contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
            config = types.GenerateContentConfig(cached_content=cache_name, **gen_kwargs)
        else:
            contents = [types.Content(role="user",
                                      parts=list(pdf_parts) + [types.Part.from_text(text=prompt)])]
            config = types.GenerateContentConfig(**gen_kwargs)
        resp = await self.client.aio.models.generate_content(
            model="gemini-3.1-pro-preview", contents=contents, config=config)
        data = json.loads(resp.text or "[]")
        out = []
        for d in data:
            theme, sub_theme = (d or {}).get("theme"), (d or {}).get("sub_theme")
            if theme and sub_theme:
                out.append({"type": "Domain", "theme": theme, "sub_theme": sub_theme,
                            "source": "Work Allocation Order"})
        return out

    async def _apply_wao_domain(
        self, frac_mappings: List[Dict[str, Any]], organization_data: Dict[str, Any],
        pdf_parts: List[types.Part], cache_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Replace each mapping's Domain competencies with WAO-derived ones (Behavioural and
        Functional kept as-is). Per-designation, concurrency-limited.

        Minimum floor (settings.DOMAIN_FROM_WAO_MIN): if the WAO yields fewer domain competencies
        than the floor, the shortfall is topped up (deduplicated) from that role's summary-based
        (PASS 2) domain set — never dropping below the floor and never padding with invented items.
        On a per-designation failure or empty WAO result, the original competencies are unchanged."""
        sem = asyncio.Semaphore(max(1, settings.DOMAIN_FROM_WAO_CONCURRENCY))
        floor = max(0, settings.DOMAIN_FROM_WAO_MIN)
        stats = {"replaced": 0, "topped_up": 0, "kept_original": 0}

        async def _one(mapping: Dict[str, Any]):
            async with sem:
                try:
                    domain = await self._generate_domain_from_wao(
                        organization_data,
                        mapping.get("designation_name", ""),
                        mapping.get("wing_division_section", ""),
                        pdf_parts, cache_name)
                except Exception as e:
                    logger.warning(f"WAO domain pass failed for '{mapping.get('designation_name')}': {e}; keeping original domain")
                    stats["kept_original"] += 1
                    return
                if not domain:
                    stats["kept_original"] += 1
                    return  # keep original (non-breaking)
                comps = mapping.get("competencies") or []
                non_domain = [c for c in comps if (c or {}).get("type") != "Domain"]
                pass2_domain = [c for c in comps if (c or {}).get("type") == "Domain"]
                if len(domain) >= floor:
                    stats["replaced"] += 1
                else:
                    # Top up to the floor from the summary-based domain (deduped). If that is also
                    # thin, keep what we have — graceful, no invented padding.
                    have = {(d.get("theme"), d.get("sub_theme")) for d in domain}
                    topup = [c for c in pass2_domain
                             if (c.get("theme"), c.get("sub_theme")) not in have]
                    domain = domain + topup[:max(0, floor - len(domain))]
                    stats["topped_up"] += 1
                mapping["competencies"] = non_domain + domain

        await asyncio.gather(*[_one(m) for m in frac_mappings])
        logger.info(f"PASS 3 summary: replaced={stats['replaced']} "
                    f"topped_up_to_floor={stats['topped_up']} kept_original={stats['kept_original']} "
                    f"(floor={floor})")
        return frac_mappings

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
            
            # Fetch document summaries for PASS 1 (only Work Allocation Order type)
            wao_summary = await self.get_documents_summary(user_id, state_center_id, department_id, document_type="Work Allocation Order")
            
            # Prepare organization data for PASS 1 with filtered summaries
            organization_data_pass1 = {
                "org_type": org_type.value,
                "state_center_id": state_center_id,
                "department_id": department_id,
                "organization_name": state_center_name,
                "department_name": department_name if department_name else "N/A",
                "docs_summary": wao_summary if wao_summary else 'N/A',
                "instruction": instruction if instruction else "N/A"
            }
            
            # ============ PASS 1: DESIGNATION EXTRACTION ============
            logger.info("STARTING PASS 1: DESIGNATION EXTRACTION")
            logger.info("PASS 1 will use only Work Allocation Order document summaries")
            
            extraction_result = await self._extract_designations(
                organization_data_pass1
            )

            designations = extraction_result.get('designations', [])
            if not designations:
                logger.warning("No designations extracted in PASS 1")
                return []
            
            logger.info(f"PASS 1 SUCCESS: {len(designations)} designations extracted")

            # ============ PASS 2: FRAC GENERATION IN BATCHES ============
            logger.info("STARTING PASS 2: FRAC GENERATION")
            
            # Fetch all document summaries for PASS 2 (no type filter)
            all_docs_summary = await self.get_documents_summary(user_id, state_center_id, department_id)
            
            # Prepare organization data for PASS 2 with all summaries
            organization_data_pass2 = {
                "org_type": org_type.value,
                "state_center_id": state_center_id,
                "department_id": department_id,
                "organization_name": state_center_name,
                "department_name": department_name if department_name else "N/A",
                "docs_summary": all_docs_summary if all_docs_summary else 'N/A',
                "instruction": instruction if instruction else "N/A"
            }
            logger.info("PASS 2 will use all document summaries")
            
            frac_mappings = await self._process_batches_parallel(
                designations,
                organization_data_pass2,
                batch_size=30
            )

            # ============ PASS 3: DOMAIN COMPETENCIES FROM RAW WAO (per designation) ============
            # Behavioural/Functional competencies from PASS 2 are kept; only Domain competencies
            # are replaced with an exhaustive, WAO-derived set. Fully guarded — any failure or a
            # missing raw WAO leaves PASS 2's (summary-based) domain competencies untouched.
            if settings.DOMAIN_FROM_WAO_ENABLED and frac_mappings:
                try:
                    pdf_parts = await self._get_wao_pdf_parts(
                        user_id, state_center_id, department_id, document_type="Work Allocation Order")
                    if pdf_parts:
                        logger.info(f"STARTING PASS 3: DOMAIN FROM WAO PDF ({len(pdf_parts)} doc(s)) "
                                    f"for {len(frac_mappings)} designations")
                        cache_name = await self._create_wao_cache(pdf_parts)
                        try:
                            frac_mappings = await self._apply_wao_domain(
                                frac_mappings, organization_data_pass2, pdf_parts, cache_name)
                        finally:
                            if cache_name:
                                await self._delete_wao_cache(cache_name)
                        logger.info("PASS 3 SUCCESS: WAO-derived domain competencies applied")
                    else:
                        logger.info("PASS 3 skipped: no WAO PDF available; keeping summary-based domain competencies")
                except Exception as e:
                    logger.warning(f"PASS 3 (WAO domain) failed: {e}; keeping summary-based domain competencies")

            logger.info("TWO-PASS ROLE MAPPING COMPLETE")
            logger.info(f"Designations Extracted: {len(designations)}")
            logger.info(f"FRAC Mappings Generated: {len(frac_mappings)}")

            return frac_mappings
        except Exception as e:
            logger.exception("Error in two-pass role mapping generation")
            raise

# Create a singleton instance
role_mapping_service = RoleMappingService()