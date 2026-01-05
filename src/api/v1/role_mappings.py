import asyncio
import json
import os
from typing import Dict, List, Optional
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from google import genai
from google.genai import types

from ...models.role_mapping import ProcessingStatus, RoleMapping
from ...models.user import User

from ...prompts.prompts import DESIGNATION_ROLE_MAPPING_PROMPT
from ...schemas.role_mapping import AddDesignationToRoleMappingRequest, RoleMappingBackgroundResponse, RoleMappingResponse, RoleMappingUpdate
from ...services.role_mapping_service import role_mapping_service

from ...core.database import get_db_session
from ...core.logger import logger
from ...core.configs import settings

from ...crud.role_mapping import crud_role_mapping
from ...crud.state_center_data import crud_state_center_data

from ...api.dependencies import get_current_active_user


router = APIRouter(tags=["Role Mappings"])

with open("data/competencies.json") as f:
    COMPETENCY_MAPPING = json.load(f)

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS
client = genai.Client(
    project=settings.GOOGLE_PROJECT_ID,
    location="us-central1",
    vertexai=True
)

async def process_role_mapping_task(
    placeholder_id: uuid.UUID,
    user_id: uuid.UUID,
    state_center_id: str,
    state_center_name: str,
    department_id: str | None,
    department_name: str | None,
    sector_name: str | None,
    instruction: str | None,
    additional_document_contents: List[bytes] | None
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
                state_center_id=state_center_id,
                state_center_name=state_center_name,
                additional_document_contents=additional_document_contents,
                department_name=department_name,
                department_id=department_id,
                sector=sector_name,
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
                state_center_id=state_center_id,
                department_id=department_id,
                state_center_name=state_center_name,
                department_name=department_name,
                sector_name=sector_name,
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
    state_center_id: str = Form(..., description="ID of the associated state/center"),
    department_id: Optional[str] = Form(None, description="ID of the associated department"),
    state_center_name: str = Form(..., description="Name of the associated state/center"),
    department_name: Optional[str] = Form(None, description="Name of the associated department"),
    sector_name: Optional[str] = Form(None, max_length=255, description="Name of the sector"),
    instruction: Optional[str] = Form(None, description="Additional instructions for role mapping generation"),
    additional_document: List[UploadFile] = File(None, description="Additional Document"),
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

        additional_document_contents = [
            await document.read()
            for document in additional_document
        ] if additional_document else []
        
        # Create Placeholder Row (Locks the process and acts as the first record)
        placeholder = RoleMapping(
            user_id=current_user.user_id,
            state_center_id=state_center_id,
            department_id=department_id,
            state_center_name=state_center_name,
            department_name=department_name,
            sector_name=sector_name,
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
            user_id=current_user.user_id,
            state_center_id=state_center_id,
            state_center_name=state_center_name,
            department_id=department_id,
            department_name=department_name,
            sector_name=sector_name,
            instruction=instruction,
            additional_document_contents=additional_document_contents
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
        state_center_data = await crud_state_center_data.get_by_state_center_and_department(input_data['state_center_id'], input_data['department_id'])
        
        # if not state_center_data:
        #     logger.warning(f"No state center data found for ID: {input_data['state_center_id']}")
        #     raise Exception("No ACBP plan or work allocation data found for this state/center")

        
        print(f"Generating role mapping for :: {input_data['designation']}")
        
        output_json_format = {
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
        }
        prompt = DESIGNATION_ROLE_MAPPING_PROMPT.format(
            organization_name=input_data.get('org_name'),
            department_name=input_data.get('dep_name'),
            designation_name=input_data.get('designation'),
            sector=input_data.get('sector_name', 'N/A'),
            instructions=input_data.get('instruction'),
            acbp_summary=state_center_data.acbp_plan_summary if state_center_data else 'N/A',
            work_allocation_summary=state_center_data.work_allocation_order_summary if state_center_data else 'N/A',
            kcm_competencies=json.dumps(COMPETENCY_MAPPING, indent=2),
            output_json_format=json.dumps(output_json_format, indent=None, separators=(',', ':'))
        )

        generate_content_config = types.GenerateContentConfig(
            temperature=0.5,
            # safety_settings=[
            #     types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF")
            # ],
            response_mime_type="application/json",
            response_schema={"type":"OBJECT","properties":{"designation_name":{"type":"STRING","description":"The official designation or job title for the role."},"wing_division_section":{"type":"STRING","description":"The organizational unit (wing, division, or section) where the role is situated."},"role_responsibilities":{"type":"ARRAY","items":{"type":"STRING"},"description":"A list of 5-8 concise, action-oriented role responsibilities."},"activities":{"type":"ARRAY","items":{"type":"STRING"},"description":"A list of 5–8 activities or tasks aligned to the role responsibilities."},"competencies":{"type":"ARRAY","items":{"type":"OBJECT","properties":{"type":{"type":"STRING","enum":["Behavioral","Functional","Domain"],"description":"The category of competency as per Karmayogi framework."},"theme":{"type":"STRING","description":"The parent theme of the competency (must come from dataset)."},"sub_theme":{"type":"STRING","description":"The sub-theme of the competency (must come from dataset)."}},"required":["type","theme","sub_theme"]},"description":"A list of competencies relevant to the role. Must include at least one Behavioral, one Functional, and one Domain competency."}},"required":["designation_name","wing_division_section","role_responsibilities","activities","competencies"]},
        )

        contents = [   
            types.Content(
                role="user", 
                parts=[
                    types.Part.from_text(text=prompt)
                ]
            )
        ]

        response = await client.aio.models.generate_content(
            model="gemini-2.5-pro",
            contents=contents,
            config=generate_content_config,
        )
        print("ADD Designation gemini metadata usage:: ", response.usage_metadata)
        text_response = response.text
        if not text_response:
            print("Gemini response was empty or not in text format.")
            return []
        parsed_response = json.loads(text_response)
        return parsed_response
    except Exception as e:
        print(f"Error generating role and responsibilities from Gemini: {e}")
        raise HTTPException(status_code=500, detail=f"Gemini error: {str(e)}")

@router.post("/role-mapping/add-designation", response_model=RoleMappingResponse, status_code=status.HTTP_201_CREATED)
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
        logger.info(f"Addig new designation generation for state_center_id: {request.state_center_id}, department_id: {request.department_id}")
        
        # Get source role mapping
        role_mapping = await crud_role_mapping.get_all_mapping(db, request.state_center_id, current_user.user_id, request.department_id)
        
        if not role_mapping:
            logger.error(f"Role mapping not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found"
            )
        
        next_sort_order = 1 if not role_mapping else role_mapping.sort_order + 1
        # 🔹 Run LLM calls in parallel
        async def generate_and_prepare(input_data: Dict):
            generated = await generate_role_and_competencies(input_data)
            return RoleMapping(
                user_id=current_user.user_id,
                state_center_id=request.state_center_id,
                state_center_name=request.state_center_name,
                department_id=request.department_id,
                department_name=request.department_name,
                instruction=request.instruction,
                sort_order=next_sort_order,
                designation_name=generated.get('designation_name'),
                wing_division_section=generated.get('wing_division_section'),
                role_responsibilities=generated.get('role_responsibilities'),
                activities=generated.get('activities'),
                competencies=generated.get('competencies')
            )
        designation_names = [request.designation_name]
        tasks = [generate_and_prepare({
            "state_center_id": request.state_center_id,
            "department_id": request.department_id,
            "org_name" : request.state_center_name,
            "dep_name" : request.department_name,
            "designation": name,
            "sector_name": None,
            "instruction": request.instruction if request.instruction else "N/A"
        }) for name in designation_names]
        designations_to_insert = await asyncio.gather(*tasks)
        new_mapping = await crud_role_mapping.create(designations_to_insert)
        return new_mapping[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating role mapping: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update role mapping"
        )

@router.get("/role-mapping/{role_mapping_id}", response_model=RoleMappingResponse)
async def get_role_mapping(
    role_mapping_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session), 
    current_user: User = Depends(get_current_active_user)
):
    """Get a specific role mapping by ID"""
    try:
        logger.info(f"Fetching role mapping with ID: {role_mapping_id}")
        
        role_mapping = await crud_role_mapping.get_by_id_and_user(db,  role_mapping_id, current_user.user_id)
        if not role_mapping:
            logger.warning(f"Role mapping with ID {role_mapping_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found"
            )
        
        logger.info(f"Retrieved role mapping for designation: {role_mapping.designation_name}")
        return role_mapping
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching role mapping: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch role mapping"
        )

@router.get("/role-mapping/state-center/{state_center_id}", response_model=List[RoleMappingResponse])
async def get_role_mappings_by_state_center(
    state_center_id: str,
    load_cbp_plans: bool = Query(False, description="Include CBP plans in the response"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Get all role mappings for a specific state/center"""
    try:
        logger.info(f"Fetching role mappings for state/center ID: {state_center_id}")
        
        role_mappings = await crud_role_mapping.get_all_completed_mapping(db, state_center_id, current_user.user_id, load_cbp_plans=load_cbp_plans)
        
        logger.info(f"Retrieved {len(role_mappings)} role mappings for state/center {state_center_id}")
        return role_mappings
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching role mappings by state/center: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch role mappings"
        )

@router.get("/role-mapping/state-center/{state_center_id}/department/{department_id}", response_model=List[RoleMappingResponse])
async def get_role_mappings_by_state_center_and_department(
    state_center_id: str,
    department_id: str,
    load_cbp_plans: bool = Query(False, description="Include CBP plans in the response"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Get all role mappings for a specific state/center and deparment"""
    try:
        logger.info(f"Fetching role mappings for state/center ID: {state_center_id} and Department ID: {department_id}")
        
        role_mappings = await crud_role_mapping.get_all_completed_mapping(db, state_center_id, current_user.user_id, department_id, load_cbp_plans=load_cbp_plans)
        logger.info(f"Retrieved {len(role_mappings)} role mappings for department {department_id}")
        return role_mappings
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error fetching role mappings by state/center and department: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch role mappings"
        )

@router.put("/role-mapping/{role_mapping_id}", response_model=RoleMappingResponse)
async def update_role_mapping(
    role_mapping_id: uuid.UUID,
    role_mapping_update: RoleMappingUpdate,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Update an existing role mapping
    
    Args:
        role_mapping_id: UUID of the role mapping to update
        role_mapping_update: Fields to update
    """
    try:
        logger.info(f"Updating role mapping with ID: {role_mapping_id}")
        
        # Get existing role mapping
        db_role_mapping = await crud_role_mapping.get_by_id_and_user(db, role_mapping_id,  current_user.user_id)
        
        if not db_role_mapping:
            logger.warning(f"Role mapping with ID {role_mapping_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found"
            )
        update_records = role_mapping_update.model_dump(exclude_unset=True)
        role_mapping = await crud_role_mapping.update(role_mapping_id, update_records)
        
        logger.info(f"Role mapping updated successfully with ID: {db_role_mapping.id}")
        return role_mapping
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating role mapping: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update role mapping"
        )

@router.delete("/role-mapping/{role_mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role_mapping(
    role_mapping_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Delete a role mapping
    
    Args:
        role_mapping_id: UUID of the role mapping to delete
        force_delete: If True, force deletion even with references (future use)
    """
    try:
        logger.info(f"Deleting role mapping with ID: {role_mapping_id}")
        
        db_role_mapping = await crud_role_mapping.get_by_id_and_user(db,role_mapping_id, current_user.user_id)
        
        if not db_role_mapping:
            logger.warning(f"Role mapping with ID {role_mapping_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found"
            )
        
        if db_role_mapping.status == ProcessingStatus.IN_PROGRESS:
            raise HTTPException(
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                detail="Cannot delete role mapping while AI generation is IN PROGRESS."
            )
                
        await crud_role_mapping.delete_by_id(db, role_mapping_id)
        
        logger.info(f"Role mapping deleted successfully with ID: {role_mapping_id}")
        return {
            "message": f"Role mapping deleted successfully with ID: {role_mapping_id}"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting role mapping: {str(e)}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete role mapping"
        )

@router.delete("/role-mapping", response_model=dict)
async def delete_role_mappings_by_state_center_and_department(
    state_center_id: str,
    department_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
):
    """
    Delete role mappings by state_center_id with optional department_id filter.

    Args:
        state_center_id: UUID of the state/center (required).
        department_id: Optional UUID of specific department to filter deletions.

    Returns:
        JSON response with deletion count and context.
    """

    logger.info(f"Delete role mappings request - state_center_id: {state_center_id}, "
                   f"department_id: {department_id}")
    
    in_progress_record = await crud_role_mapping.get_in_progress_mapping(db,state_center_id,  current_user.user_id, department_id)

    if in_progress_record:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="Cannot delete role mappings while AI generation is IN PROGRESS for this State/Department."
        )

    # ✅ Perform bulk delete in one shot
    deleted_count = await crud_role_mapping.delete_existing_mappings(db,state_center_id,  current_user.user_id, department_id)

    if deleted_count == 0:
        return {
            "message": f"No role mappings found for given state/center or department",
            "deleted_count": 0,
            "context": {
                "state_center_id": str(state_center_id),
                "department_id": str(department_id) if department_id else None
            },
        }

    return {
        "message": f"Successfully deleted {deleted_count} role mappings.",
        "deleted_count": deleted_count,
        "context": {
            "state_center_id": str(state_center_id),
            "department_id": str(department_id) if department_id else None
        },
    }
