# src/role_mapping_service.py
import json
from typing import Dict, Any, List, Literal, Optional
import uuid
import asyncio
from pydantic import BaseModel, Field

from ...schemas.role_mapping import OrgType
from ...core.configs import settings
from ...prompts.v3.prompts import (
    DESIGNATION_EXTRACTION_PROMPT,
    ROLE_MAPPING_PROMPT_CENTRE,
    ROLE_MAPPING_PROMPT_STATE,
)
from ...crud.document import crud_document
from ...core.logger import logger
from ..llm import GenerationConfig, Message, get_llm

with open("data/withidentifier_competencies.json") as f:
    COMPETENCY_MAPPING = json.load(f)

# Deterministic KCM canonicalization index (id -> exact {type, theme, sub_theme}).
# The LLM selects a `competency_id`; the server rebuilds the authoritative name from
# this table, so a Behavioural/Functional competency can never be mixed, type-swapped,
# renamed, or invented outside KCM. Domain competencies are outside KCM and untouched.
KCM_BY_ID = {
    e["competency_id"]: {
        "competency_id": e["competency_id"],
        "type": e["type"],
        "theme": e["theme"],
        "sub_theme": e["sub_theme"],
    }
    for e in COMPETENCY_MAPPING
}
# Fallback index for competencies that arrive without a (valid) id: exact (type, theme,
# sub_theme) match snaps back to canonical; a paired (theme, sub_theme) match recovers a
# swapped type. Keyed by casefolded strings to tolerate whitespace/case drift only.
_KCM_BY_TRIPLE = {
    (e["type"].casefold(), e["theme"].casefold(), e["sub_theme"].casefold()): e["competency_id"]
    for e in COMPETENCY_MAPPING
}
_KCM_BY_PAIR = {
    (e["theme"].casefold(), e["sub_theme"].casefold()): e["competency_id"]
    for e in COMPETENCY_MAPPING
}


def _norm_type(t: str) -> str:
    """Map model/KCM type spellings to the KCM canonical ('Behavioural'/'Functional'/'Domain')."""
    t = (t or "").strip().casefold()
    if t.startswith("behav"):
        return "Behavioural"
    if t.startswith("func"):
        return "Functional"
    return "Domain"


def canonicalize_competencies(raw_competencies: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Snap every Behavioural/Functional competency back to its exact KCM entry.

    Resolution order per B/F competency:
      1. valid `competency_id` -> use it (id is authoritative).
      2. exact (type, theme, sub_theme) match -> recover id.
      3. exact (theme, sub_theme) pair match -> recover id AND correct a swapped type.
      4. none -> DROP (out-of-KCM hallucination) and record it.
    Domain competencies pass through unchanged. Duplicates (same canonical id) are removed.

    Returns {"competencies": [...canonical...], "metrics": {...}} where metrics measure
    what the OLD (name-copy) system would have leaked: name/type/id mismatches and drops.
    """
    kept: List[Dict[str, Any]] = []
    seen_ids: set = set()
    metrics = {
        "bf_total": 0, "resolved": 0, "dropped": 0,
        "name_mismatch": 0, "type_mismatch": 0, "id_missing_or_bad": 0,
        "clean": 0,            # LLM emitted a valid id AND echoed type/theme/sub exactly (no LLM error)
        "dropped_items": [],
        "records": [],         # per-competency raw-LLM-output vs canonical decision (for the hallucination report)
    }
    for c in (raw_competencies or []):
        ctype = _norm_type(c.get("type", ""))
        if ctype == "Domain":
            kept.append({k: v for k, v in c.items() if k != "competency_id"})
            continue
        metrics["bf_total"] += 1
        llm_id = (c.get("competency_id") or "").strip()
        llm_theme = (c.get("theme") or "").strip()
        llm_sub = (c.get("sub_theme") or "").strip()

        id_valid = llm_id in KCM_BY_ID
        cid = None
        if id_valid:
            cid = llm_id
        else:
            if llm_id:
                metrics["id_missing_or_bad"] += 1
            cid = _KCM_BY_TRIPLE.get((ctype.casefold(), llm_theme.casefold(), llm_sub.casefold())) \
                or _KCM_BY_PAIR.get((llm_theme.casefold(), llm_sub.casefold()))

        # --- classify the RAW LLM output (before we fix anything) ---
        if not cid:
            status = "hallucinated_dropped"          # LLM output not resolvable to any KCM entry
        else:
            canon = KCM_BY_ID[cid]
            name_bad = (llm_theme != canon["theme"] or llm_sub != canon["sub_theme"])
            type_bad = (ctype != canon["type"])
            if id_valid and not name_bad and not type_bad:
                status = "clean"                     # LLM fully correct on its own
            elif id_valid and type_bad:
                status = "type_swapped"              # valid id but LLM wrote wrong type
            elif id_valid and name_bad:
                status = "renamed"                   # valid id but LLM altered/paraphrased the name
            elif type_bad:
                status = "no_id_type_recovered"      # no valid id; recovered via name, type was wrong
            elif name_bad:
                status = "no_id_recovered"           # no valid id; recovered via (theme,sub) pair
            else:
                status = "no_id_named_correct"       # LLM named a real entry correctly but omitted the id

        rec = {"raw_id": llm_id, "raw_type": c.get("type", ""), "raw_theme": llm_theme,
               "raw_sub": llm_sub, "final_id": cid or "", "status": status}
        metrics["records"].append(rec)

        if not cid:
            metrics["dropped"] += 1
            metrics["dropped_items"].append({"type": ctype, "theme": llm_theme, "sub_theme": llm_sub, "competency_id": llm_id})
            continue

        canon = KCM_BY_ID[cid]
        if status == "clean":
            metrics["clean"] += 1
        if llm_theme != canon["theme"] or llm_sub != canon["sub_theme"]:
            metrics["name_mismatch"] += 1
        if ctype != canon["type"]:
            metrics["type_mismatch"] += 1

        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        metrics["resolved"] += 1
        out = {"type": canon["type"], "theme": canon["theme"], "sub_theme": canon["sub_theme"], "competency_id": cid}
        if c.get("source") is not None:
            out["source"] = c["source"]
        kept.append(out)

    return {"competencies": kept, "metrics": metrics}


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
                "competency_id": "KCM id e.g. BEH-07 / FUN-23 (REQUIRED for Behavioural & Functional; omit for Domain)",
                "type": "Behavioural | Functional | Domain",
                "theme": "string",
                "sub_theme": "string"
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
                "competency_id": "KCM id e.g. BEH-07 / FUN-23 (REQUIRED for Behavioural & Functional; omit for Domain)",
                "type": "Behavioural | Functional | Domain",
                "theme": "string",
                "sub_theme": "string"
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
    competency_id: Optional[str] = Field(default=None, description="KCM competency id (e.g. BEH-07 / FUN-23). REQUIRED for Behavioural & Functional; omit for Domain.")
    type: Literal["Behavioural", "Functional", "Domain"] = Field(description="Competency type: Behavioural, Functional, or Domain")
    theme: str = Field(description="Competency theme")
    sub_theme: str = Field(description="Competency sub theme")

class FRACRoleMapping(BaseModel):
    designation_name: str = Field(description="Official designation name")
    wing_division_section: str = Field(description="Wing/division/section the designation belongs to")
    role_responsibilities: List[str] = Field(description="Flat list of role responsibilities as strings")
    activities: List[str] = Field(description="Flat list of activity strings")
    sort_order: int = Field(description="Hierarchy sort order, strictly increasing from 1")
    competencies: List[FRACCompetency] = Field(description="Flat list of competency objects.")
    source: Optional[List[str]] = Field(default=None, description="Source references")
class FRACBatchResponse(BaseModel):
    mappings: List[FRACRoleMapping] = Field(description="List of FRAC role mappings for all designations in the batch")
class RoleMappingService:
    """Service for generating role mappings using an LLM"""

    def __init__(self):
        self.llm = get_llm()

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
        logger.info(f"PASS 1: Extracting designations for {organization_data.get('organization_name')}")

        prompt = """
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
        contents = [Message.user(prompt)]

        generate_content_config = GenerationConfig(
            system_instruction=DESIGNATION_EXTRACTION_PROMPT,
            temperature=0.1,   # Very low — factual extraction, no creativity
            top_p=0.85, # Restrict to high-probability tokens
        )

        extraction_response = await self.llm.generate_structured(
            contents,
            model=settings.GEMINI_FLASH_MODEL_NAME,
            schema=DesignationExtractionResponse,
            config=generate_content_config,
        )

        return {
            "designations": [d.model_dump() for d in extraction_response.designations]
        }

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
            is_state = organization_data["org_type"] == OrgType.state.value
            logger.info(f"PASS 2 - Batch {batch_number}: Processing {len(designations_batch)} designations")
            logger.info(f"Role Mapping is using prompt :: {'STATE_PROMPT' if is_state else 'CENTER_PROMPT'}")
            PROMPT = ROLE_MAPPING_PROMPT_STATE if is_state else ROLE_MAPPING_PROMPT_CENTRE
            output_json_format = state_json_output if is_state else center_json_output

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

            contents = [Message.user(base_prompt)]

            generate_content_config = GenerationConfig(
                temperature=0.3,
                top_p=0.90,
            )

            batch_response = await self.llm.generate_structured(
                contents,
                model=settings.GEMINI_PRO_MODEL_NAME,
                schema=FRACBatchResponse,
                config=generate_content_config,
            )

            validated_response = [record.model_dump() for record in batch_response.mappings]

            logger.info(f"Batch {batch_number}: Successfully generated {len(validated_response)} FRAC mappings")
            return validated_response
        except Exception as e:
            logger.exception(f"Batch {batch_number}: Error generating FRAC mapping")
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
            parts.append(f"<document_summary_{idx}>\n Document Type: {doc.document_type} \n Summary: {summary}\n</document_summary_{idx}>")

        return "\n\n".join(parts)

    @staticmethod
    def _normalize(text: Optional[str]) -> str:
        """Lowercase/trim for tolerant comparison"""
        return (text or "").strip().lower()

    @staticmethod
    def _norm_type(t: str) -> str:
        """Map model/KCM type spellings to the KCM canonical ('Behavioural'/'Functional'/'Domain')."""
        t = (t or "").strip().casefold()
        if t.startswith("behav"):
            return "Behavioural"
        if t.startswith("func"):
            return "Functional"
        return "Domain"

    def _reconcile_competency_against_kcm( self, raw_competencies: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Snap every Behavioural/Functional competency back to its exact KCM entry.

        Resolution order per B/F competency:
        1. valid `competency_id` -> use it (id is authoritative).
        2. exact (type, theme, sub_theme) match -> recover id.
        3. exact (theme, sub_theme) pair match -> recover id AND correct a swapped type.
        4. none -> DROP (out-of-KCM hallucination) and record it.
        Domain competencies pass through unchanged. Duplicates (same canonical id) are removed.

        Returns {"competencies": [...canonical...], "metrics": {...}} where metrics measure
        what the OLD (name-copy) system would have leaked: name/type/id mismatches and drops.
        """
        kept: List[Dict[str, Any]] = []
        seen_ids: set = set()
        metrics = {
            "bf_total": 0, "resolved": 0, "dropped": 0,
            "name_mismatch": 0, "type_mismatch": 0, "id_missing_or_bad": 0,
            "clean": 0,            # LLM emitted a valid id AND echoed type/theme/sub exactly (no LLM error)
            "dropped_items": [],
            "records": [],         # per-competency raw-LLM-output vs canonical decision (for the hallucination report)
        }
        for c in (raw_competencies or []):
            ctype = self._norm_type(c.get("type", ""))
            if ctype == "Domain":
                kept.append({k: v for k, v in c.items() if k != "competency_id"})
                continue
            metrics["bf_total"] += 1
            llm_id = (c.get("competency_id") or "").strip()
            llm_theme = (c.get("theme") or "").strip()
            llm_sub = (c.get("sub_theme") or "").strip()

            id_valid = llm_id in KCM_BY_ID
            cid = None
            if id_valid:
                cid = llm_id
            else:
                if llm_id:
                    metrics["id_missing_or_bad"] += 1
                cid = _KCM_BY_TRIPLE.get((ctype.casefold(), llm_theme.casefold(), llm_sub.casefold())) \
                    or _KCM_BY_PAIR.get((llm_theme.casefold(), llm_sub.casefold()))

            # --- classify the RAW LLM output (before we fix anything) ---
            if not cid:
                status = "hallucinated_dropped"          # LLM output not resolvable to any KCM entry
            else:
                canon = KCM_BY_ID[cid]
                name_bad = (llm_theme != canon["theme"] or llm_sub != canon["sub_theme"])
                type_bad = (ctype != canon["type"])
                if id_valid and not name_bad and not type_bad:
                    status = "clean"                     # LLM fully correct on its own
                elif id_valid and type_bad:
                    status = "type_swapped"              # valid id but LLM wrote wrong type
                elif id_valid and name_bad:
                    status = "renamed"                   # valid id but LLM altered/paraphrased the name
                elif type_bad:
                    status = "no_id_type_recovered"      # no valid id; recovered via name, type was wrong
                elif name_bad:
                    status = "no_id_recovered"           # no valid id; recovered via (theme,sub) pair
                else:
                    status = "no_id_named_correct"       # LLM named a real entry correctly but omitted the id

            rec = {"raw_id": llm_id, "raw_type": c.get("type", ""), "raw_theme": llm_theme,
                "raw_sub": llm_sub, "final_id": cid or "", "status": status}
            metrics["records"].append(rec)

            if not cid:
                metrics["dropped"] += 1
                metrics["dropped_items"].append({"type": ctype, "theme": llm_theme, "sub_theme": llm_sub, "competency_id": llm_id})
                continue

            canon = KCM_BY_ID[cid]
            if status == "clean":
                metrics["clean"] += 1
            if llm_theme != canon["theme"] or llm_sub != canon["sub_theme"]:
                metrics["name_mismatch"] += 1
            if ctype != canon["type"]:
                metrics["type_mismatch"] += 1

            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            metrics["resolved"] += 1
            out = {"type": canon["type"], "theme": canon["theme"], "sub_theme": canon["sub_theme"], "competency_id": cid}
            if c.get("source") is not None:
                out["source"] = c["source"]
            kept.append(out)

        return {"competencies": kept, "metrics": metrics}

    def reconcile_role_mappings_with_kcm(
        self, frac_mappings: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Cross-verify every generated role mapping's competencies against the KCM dataset
        (data/withidentifier_competencies.json), correcting mismatches per _reconcile_competency_against_kcm.

        Domain competencies are skipped (not part of the KCM Behavioural/Functional master)
        and passed through unchanged.
        """
        # Deterministic KCM canonicalization: snap every Behavioural/Functional
        # competency back to its exact KCM entry by id (drops out-of-KCM items,
        # corrects swapped types, restores altered names). Domain is untouched.
        agg = {"bf_total": 0, "resolved": 0, "dropped": 0, "name_mismatch": 0, "type_mismatch": 0, "id_missing_or_bad": 0, "clean": 0}
        for mapping in frac_mappings:
            result = self._reconcile_competency_against_kcm(mapping.get("competencies", []))
            mapping["competencies"] = result["competencies"]
            for k in agg:
                agg[k] += result["metrics"][k]
            for item in result["metrics"]["dropped_items"]:
                logger.warning(f"Dropped out-of-KCM competency for '{mapping.get('designation_name')}': {item}")

        logger.info(
            f"KCM canonicalization — B/F={agg['bf_total']} clean(LLM-correct)={agg['clean']} "
            f"resolved={agg['resolved']} dropped={agg['dropped']} name_mismatch={agg['name_mismatch']} "
            f"type_mismatch={agg['type_mismatch']} bad_id={agg['id_missing_or_bad']}"
        )

        # DB persists the US spelling "Behavioral" even though the KCM dataset and all
        # reconciliation above use "Behavioural" — normalize only at this final boundary.
        for mapping in frac_mappings:
            for competency in mapping.get("competencies") or []:
                if self._normalize(competency.get("type")) == "behavioural":
                    competency["type"] = "Behavioral"

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
        Generate role mapping with three-pass approach:
        PASS 1: Extract all designations
        PASS 2: Generate FRAC mappings in batches
        PASS 3: Reconcile Behavioural/Functional competencies against the KCM dataset

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
            logger.info(f"Starting three-pass role mapping generation for user {user_id}, state_center_id {state_center_id}, department_id {department_id}")

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
                raise Exception("No designations extracted in PASS 1; cannot proceed to PASS 2")

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
                batch_size=settings.ROLE_MAPPING_BATCH_SIZE
            )

            # ============ PASS 3: KCM RECONCILIATION (Behavioural/Functional only) ============
            # Cross-verify every Behavioural/Functional competency against the KCM dataset
            # (data/withidentifier_competencies.json), correcting LLM drift (swapped theme/sub_theme,
            # wrong type, missing/invalid competency_id) instead of trusting it blindly. Domain
            # competencies are left untouched (not part of the KCM master).
            frac_mappings = self.reconcile_role_mappings_with_kcm(frac_mappings)

            logger.info("THREE-PASS ROLE MAPPING COMPLETE")
            logger.info(f"Designations Extracted: {len(designations)}")
            logger.info(f"FRAC Mappings Generated: {len(frac_mappings)}")

            return frac_mappings
        except Exception as e:
            raise

# Create a singleton instance
role_mapping_service = RoleMappingService()
