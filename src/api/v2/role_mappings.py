import asyncio
import json
from typing import Dict, List, Optional
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.role_mapping import ProcessingStatus, RoleMapping
from ...models.user import User

from ...prompts.v2.prompts import DESIGNATION_ROLE_MAPPING_PROMPT
from ...schemas.role_mapping import AddDesignationToRoleMappingRequest, OrgType, RoleMappingBackgroundResponse, RoleMappingResponse, RoleMappingUpdate, RoleMappingWithoutCBP
from ...services.v2.role_mapping_service import role_mapping_service
from ...services.v3.role_mapping_service import role_mapping_service as role_mapping_service_v3
from ...services.llm import GenerationConfig, Message, get_llm

from ...core.database import get_db_session
from ...core.logger import logger
from ...core.configs import settings

from ...crud.role_mapping import crud_role_mapping
from ...api.dependencies import get_current_active_user


router = APIRouter(tags=["Role Mappings"])

with open("data/competencies.json") as f:
    COMPETENCY_MAPPING = json.load(f)

async def process_role_mapping_task(
    placeholder_id: uuid.UUID,
    org_type:OrgType,
    user_id: uuid.UUID,
    state_center_id: str,
    state_center_name: str,
    department_id: str | None,
    department_name: str | None,
    instruction: str | None
):
    """
    Background task.
    1. Sets status to IN_PROGRESS (already set by API, but we confirm existence).
    2. Calls AI service.
    3. On Success: Updates the placeholder row with the first result and adds new rows for the rest.
    4. On Failure: Updates placeholder status to FAILED.
    """
    try:
        logger.info(f"Task Started: Processing for placeholder {placeholder_id}")
        
        # 1. Fetch the Placeholder Row
        placeholder_row = await crud_role_mapping.get_by_id(placeholder_id)
        if not placeholder_row:
            logger.error(f"Placeholder row {placeholder_id} not found. Task Aborted.")
            return

        # 2. Generate Data (Blocking Call)
        try:
            generated_data_list = await role_mapping_service.generate_role_mapping(
                user_id=user_id,
                org_type=org_type,
                state_center_id=state_center_id,
                state_center_name=state_center_name,
                department_name=department_name,
                department_id=department_id,
                instruction=instruction
            )
        except Exception as e:
            generated_data_list = None

        if not generated_data_list:
            update_records = {
                'status': ProcessingStatus.FAILED,
                'error_message': "AI Service returned no role mappings."
            }
            await crud_role_mapping.update(placeholder_id, update_records)
            return

        # 3. Update the Placeholder to become the First Valid Record
        # The placeholder ID acts as the persistent reference for the user
        first_record_data = generated_data_list[0]
        await crud_role_mapping.update(
            placeholder_id,
            {
                'status':ProcessingStatus.COMPLETED,
                'designation_name': first_record_data.get('designation_name'),
                'wing_division_section': first_record_data.get('wing_division_section'),
                'role_responsibilities':first_record_data.get('role_responsibilities'),
                'activities': first_record_data.get('activities'),
                'competencies': first_record_data.get('competencies'),
                'sort_order': first_record_data.get('sort_order'),
                'error_message': None
            }
        )
        # 4. Insert the Remaining Records (if any)
        new_mappings = []
        for data in generated_data_list[1:]:
            new_mapping = RoleMapping(
                user_id=user_id,
                org_type=org_type,
                state_center_id=state_center_id,
                department_id=department_id,
                state_center_name=state_center_name,
                department_name=department_name,
                instruction=instruction,
                status=ProcessingStatus.COMPLETED, # Immediately valid
                designation_name=data.get('designation_name'),
                wing_division_section=data.get('wing_division_section'),
                role_responsibilities=data.get('role_responsibilities'),
                activities=data.get('activities'),
                competencies=data.get('competencies'),
                sort_order=data.get('sort_order')
            )
            new_mappings.append(new_mapping)

        await crud_role_mapping.create(new_mappings)
        logger.info(f"Task Completed. Updated placeholder {placeholder_id} and added {len(new_mappings)} new rows.")
    except Exception as e:  
        error_msg = str(e)
        logger.error(f"Role Mapping Task Failed: {error_msg}")
        
        # 5. Update Status to FAILED on the placeholder
        try:
            # Re-query needed if rollback occurred
            update_records = {
                'status': ProcessingStatus.FAILED,
                'error_message': error_msg
            }
            await crud_role_mapping.update(placeholder_id, update_records)
        except Exception as inner_e:
            logger.error(f"Failed to update error status for role mapping {placeholder_id} job: {inner_e}")

# Role Mapping APIs
@router.post("/role-mapping/generate", response_model=RoleMappingBackgroundResponse, status_code=status.HTTP_202_ACCEPTED)
async def generate_role_mapping(
    background_tasks: BackgroundTasks,
    org_type: OrgType = Form(..., description="Organization type: ministry or state"),
    state_center_id: str = Form(..., description="ID of the associated state/center"),
    department_id: Optional[str] = Form(None, description="ID of the associated department"),
    state_center_name: str = Form(..., description="Name of the associated state/center"),
    department_name: Optional[str] = Form(None, description="Name of the associated department"),
    instruction: Optional[str] = Form(None, description="Additional instructions for role mapping generation"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Generate role mapping based on state/center data, department, and sector.
    Uses AI to analyze ACBP plan and work allocation data to generate designations, roles, activities, and competencies.
    """
    try:
        logger.info(f"Starting role mapping generation for state_center_id: {state_center_id}, department_id: {department_id}")

        # Check if role mapping already exists
        existing_role_mapping = await crud_role_mapping.get_all_mapping(db, state_center_id, current_user.user_id, department_id)
        
        if existing_role_mapping:
            current_status = existing_role_mapping.status
            
            if current_status == ProcessingStatus.IN_PROGRESS:
                return RoleMappingBackgroundResponse(
                    is_existing=False,
                    status=ProcessingStatus.IN_PROGRESS, 
                    message="Generation is already IN PROGRESS for this State/Center."
                )
            
            if current_status == ProcessingStatus.COMPLETED:
                logger.info(f"Role mapping already exists")
                existing_role_mapping = await crud_role_mapping.get_all_completed_mapping(db, state_center_id, current_user.user_id, department_id)
                return JSONResponse(
                    status_code=status.HTTP_201_CREATED,
                    content=RoleMappingBackgroundResponse(
                        is_existing=True,
                        message="Role mapping generated successfully",
                        status=ProcessingStatus.COMPLETED,
                        role_mappings=existing_role_mapping
                    ).model_dump(mode="json")
                )
            
            if current_status == ProcessingStatus.FAILED:
                logger.info("Found failed records. Cleaning up to retry...")
                # Delete all records matching the filter to ensure a clean slate
                await crud_role_mapping.delete_existing_mappings(db, state_center_id, current_user.user_id, department_id)
        
        # Create Placeholder Row (Locks the process and acts as the first record)
        placeholder = RoleMapping(
            user_id=current_user.user_id,
            org_type=org_type,
            state_center_id=state_center_id,
            department_id=department_id,
            state_center_name=state_center_name,
            department_name=department_name,
            instruction=instruction,
            status=ProcessingStatus.IN_PROGRESS,
            # Dummy values for non-nullable fields
            designation_name="Generating...", 
            wing_division_section="Generating...",
            role_responsibilities=[],
            activities=[],
            competencies=[]
        )
    
        placeholder = await crud_role_mapping.create([placeholder])
    
        logger.info("Dispatching AI service background task")
        
        background_tasks.add_task(
            process_role_mapping_task,
            placeholder_id=placeholder[0].id,
            org_type=org_type,
            user_id=current_user.user_id,
            state_center_id=state_center_id,
            state_center_name=state_center_name,
            department_id=department_id,
            department_name=department_name,
            instruction=instruction
        )

        return {
            "is_existing":False,
            "message": "Role mapping generation started in background.",
            "status": ProcessingStatus.IN_PROGRESS
        }
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error initiating role mapping: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiate role mapping: {str(e)}"
        )

async def generate_role_and_competencies(input_data):
    # Build strict prompt
    try:
        docs_summary = await role_mapping_service.get_documents_summary(input_data['user_id'], input_data['state_center_id'], input_data['department_id'])
        
        # if not docs_summary:
        #     logger.warning(f"No document data found for ID: {input_data['state_center_id']}")
        #     raise Exception("No document data found for this state/center")

        
        logger.info(f"Generating role and competencies for designation: {input_data.get('designation')}")
        
        output_json_format = {
            "designation_name": "[Designation Name]",
            "wing_division_section": "[Wing/Division/Section]",
            "role_responsibilities": "[List of Role Responsibilities]",
            "activities": "[List of Activities]",
            "competencies": [
                {
                    "type": "[Behavioural/Functional/Domain]",
                    "theme": "[Competency Theme]",
                    "sub_theme": "[Competency Sub-theme]",
                }
            ],
            "source": "[Primary document summaries, KCM, AI Suggested]"
        }
        prompt = DESIGNATION_ROLE_MAPPING_PROMPT.format(
            organization_name=input_data.get('org_name'),
            department_name=input_data.get('dep_name'),
            designation_name=input_data.get('designation'),
            sector=input_data.get('sector_name', 'N/A'),
            instructions=input_data.get('instruction'),
            primary_summary=docs_summary or 'N/A',
            kcm_competencies=json.dumps(COMPETENCY_MAPPING, indent=2),
            output_json_format=json.dumps(output_json_format, indent=None, separators=(',', ':'))
        )

        schema = {"type":"OBJECT","properties":{"designation_name":{"type":"STRING","description":"The official designation or job title for the role."},"wing_division_section":{"type":"STRING","description":"The organizational unit (wing, division, or section) where the role is situated."},"role_responsibilities":{"type":"ARRAY","items":{"type":"STRING"},"description":"A list of 5-8 concise, action-oriented role responsibilities."},"activities":{"type":"ARRAY","items":{"type":"STRING"},"description":"A list of 5–8 activities or tasks aligned to the role responsibilities."},"competencies":{"type":"ARRAY","items":{"type":"OBJECT","properties":{"type":{"type":"STRING","enum":["Behavioural","Functional","Domain"],"description":"The category of competency as per Karmayogi framework."},"theme":{"type":"STRING","description":"The parent theme of the competency (must come from dataset)."},"sub_theme":{"type":"STRING","description":"The sub-theme of the competency (must come from dataset)."}},"required":["type","theme","sub_theme"]},"description":"A list of competencies relevant to the role. Must include at least one Behavioural, one Functional, and one Domain competency."}},"required":["designation_name","wing_division_section","role_responsibilities","activities","competencies"]}

        contents = [Message.user(prompt)]

        generate_content_config = GenerationConfig(temperature=0.5)

        parsed_response = await get_llm().generate_structured(
            contents,
            model=settings.GEMINI_PRO_MODEL_NAME,
            schema=schema,
            config=generate_content_config,
        )
        return parsed_response
    except Exception as e:
        logger.exception(f"Background task failed for designation: {input_data.get('designation')}")
        raise

@router.post("/role-mapping/add-designation", response_model=RoleMappingWithoutCBP, status_code=status.HTTP_201_CREATED)
async def add_designation_to_role_mapping(
    request: AddDesignationToRoleMappingRequest, db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
    ):
    """
    Add a new designation by copying from an existing role mapping

    Args:
        request: Add designation request with source role mapping ID and new designation details
        
    Returns:
        Details of the newly created role mapping with copied data
    """
    try:
        logger.info(f"Add Designation request received for user {current_user.user_id}, state_center_id {request.state_center_id}, department_id {request.department_id}, designation_name {request.designation_name}")
        
        # Get source role mapping
        role_mapping = await crud_role_mapping.get_all_mapping(db, request.state_center_id, current_user.user_id, request.department_id)
        
        if not role_mapping:
            logger.warning(f"Role mapping not found for user {current_user.user_id}, state_center_id {request.state_center_id}, department_id {request.department_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found for the specified state/center and department."
            )
        
        # 🔹 Run LLM calls in parallel (sort_order assigned atomically on insert)
        async def generate_and_prepare(input_data: Dict):
            generated = await generate_role_and_competencies(input_data)
            return RoleMapping(
                user_id=current_user.user_id,
                org_type=role_mapping.org_type,
                state_center_id=request.state_center_id,
                state_center_name=request.state_center_name,
                department_id=request.department_id,
                department_name=request.department_name,
                instruction=request.instruction,
                designation_name=generated.get('designation_name'),
                wing_division_section=generated.get('wing_division_section'),
                role_responsibilities=generated.get('role_responsibilities'),
                activities=generated.get('activities'),
                competencies=generated.get('competencies')
            )
        designation_names = [request.designation_name]
        tasks = [generate_and_prepare({
            "user_id": current_user.user_id,
            "state_center_id": request.state_center_id,
            "department_id": request.department_id,
            "org_name" : request.state_center_name,
            "dep_name" : request.department_name,
            "designation": name,
            "sector_name": None,
            "instruction": request.instruction if request.instruction else "N/A"
        }) for name in designation_names]
        designations_to_insert = await asyncio.gather(*tasks)
        
        # Derive Domain competencies for the manually-added designation(s) from the raw WAO —
        # the SAME PASS 3 logic as bulk generation (reuses the WAO PDF + context cache + min floor).
        # Only the Domain slice is replaced; Behavioural/Functional are untouched. Fully guarded:
        # a missing/unreadable WAO or any error keeps the summary-based domain, so neither the flow
        # nor the response contract is affected.
        if settings.DOMAIN_FROM_WAO_ENABLED and designations_to_insert:
            try:
                pdf_parts = await role_mapping_service_v3._get_wao_pdf_parts(
                    current_user.user_id, request.state_center_id, request.department_id,
                    document_type="Work Allocation Order")
                if pdf_parts:
                    cache_name = (await role_mapping_service_v3._create_wao_cache(pdf_parts)
                                  if len(designations_to_insert) > 1 else None)
                    try:
                        wrapped = [{"designation_name": m.designation_name,
                                    "wing_division_section": m.wing_division_section,
                                    "competencies": m.competencies or []}
                                   for m in designations_to_insert]
                        await role_mapping_service_v3._apply_wao_domain(
                            wrapped,
                            {"organization_name": request.state_center_name,
                             "department_name": request.department_name},
                            pdf_parts, cache_name)
                        for m, w in zip(designations_to_insert, wrapped):
                            m.competencies = w["competencies"]
                    finally:
                        if cache_name:
                            await role_mapping_service_v3._delete_wao_cache(cache_name)
            except Exception as e:
                logger.warning(f"WAO domain enrichment skipped for add-designation: {e}; keeping summary-based domain")

        # Cross-verify Behavioural/Functional competencies against the KCM dataset, correcting
        # any theme/sub_theme/type drift from the LLM (reuses the v3 reconciliation logic).
        if designations_to_insert:
            wrapped = [{"competencies": m.competencies or []} for m in designations_to_insert]
            wrapped = role_mapping_service_v3.reconcile_role_mappings_with_kcm(wrapped)
            for m, w in zip(designations_to_insert, wrapped):
                m.competencies = w["competencies"]

        # Assign sort_order atomically to prevent duplicates under parallel calls
        new_mapping = await crud_role_mapping.create_with_next_sort_order(
            designations_to_insert,
            state_center_id=request.state_center_id,
            user_id=current_user.user_id,
            department_id=request.department_id
        )
        return new_mapping[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to add designation to role mapping")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add designation to role mapping"
        )
