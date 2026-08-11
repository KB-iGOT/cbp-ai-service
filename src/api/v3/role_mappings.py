import json
from typing import Optional
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.role_mapping import ProcessingStatus, RoleMapping
from ...models.user import User

from ...schemas.role_mapping import OrgType, RoleMappingBackgroundResponse
from ...services.v3.role_mapping_service import role_mapping_service

from ...core.database import get_db_session
from ...core.logger import logger
from ...core.configs import settings

from ...crud.role_mapping import crud_role_mapping
from ...services.designation_matcher_service import designation_matcher_service
from ...core.database import sessionmanager
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
        logger.info(f"Role mapping processing started for placeholder {placeholder_id}")

        # 1. Fetch the Placeholder Row
        placeholder_row = await crud_role_mapping.get_by_id(placeholder_id)
        
        if not placeholder_row:
            raise Exception(f"Placeholder row not found for ID: {placeholder_id}. Aborting task.")

        # 2. Generate Data (Blocking Call)
        generated_data_list = await role_mapping_service.generate_role_mapping(
            user_id=user_id,
            org_type=org_type,
            state_center_id=state_center_id,
            state_center_name=state_center_name,
            department_name=department_name,
            department_id=department_id,
            instruction=instruction
        )

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

        # 5. Auto-match all completed designations against iGOT master
        try:
            async with sessionmanager.session() as db:
                all_completed = await crud_role_mapping.get_all_completed_mapping(
                    db, state_center_id, user_id, department_id
                )
            records_to_match = [rm for rm in (all_completed or []) if not rm.igot_designation_id]
            if records_to_match:
                designation_names = list({rm.designation_name for rm in records_to_match})
                async with sessionmanager.session() as db:
                    all_match_results = await designation_matcher_service.match(db, designation_names)
                match_dict = {m["input_designation"].lower(): m for m in all_match_results}

                bulk_updates = []
                for rm in records_to_match:
                    match_data = match_dict.get(rm.designation_name.lower())
                    if match_data:
                        bulk_updates.append({
                            "role_mapping_id": rm.id,
                            "igot_designation_name": match_data.get("designation"),
                            "igot_designation_id": match_data.get("id"),
                        })

                if bulk_updates:
                    async with sessionmanager.session() as db:
                        updated = await crud_role_mapping.bulk_update_designation_matching(db, bulk_updates)
                    logger.info(f"Role mapping processing auto-matched {updated}/{len(records_to_match)} designations for placeholder {placeholder_id}")
        except Exception as match_err:
            logger.exception(f"Role mapping processing auto designation match failed (non-critical) for placeholder {placeholder_id}")

        logger.info(f"Role mapping processing completed for placeholder {placeholder_id}, added {len(new_mappings)} new rows")
    except Exception as e:
        error_msg = str(e)
        logger.exception(f"Role mapping processing failed for placeholder {placeholder_id}")

        # 5. Update Status to FAILED on the placeholder
        try:
            # Re-query needed if rollback occurred
            update_records = {
                'status': ProcessingStatus.FAILED,
                'error_message': error_msg
            }
            await crud_role_mapping.update(placeholder_id, update_records)
        except Exception as inner_e:
            logger.error(f"Role mapping processing failed to update FAILED status for placeholder {placeholder_id}: {inner_e}")

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
        logger.info(f"Received role mapping generation request for user {current_user.user_id}, state_center_id {state_center_id}, department_id {department_id}")

        # Check if role mapping already exists
        existing_role_mapping = await crud_role_mapping.get_all_mapping(db, state_center_id, current_user.user_id, department_id)
        
        if existing_role_mapping:
            current_status = existing_role_mapping.status
            
            if current_status == ProcessingStatus.IN_PROGRESS:
                return RoleMappingBackgroundResponse(
                    is_existing=False,
                    status=ProcessingStatus.IN_PROGRESS, 
                    message="Role mapping generation is already in progress. Please check back later."
                )
            
            if current_status == ProcessingStatus.COMPLETED:
                logger.info("Role mapping already completed, returning existing data.")
                return JSONResponse(
                    status_code=status.HTTP_201_CREATED,
                    content=RoleMappingBackgroundResponse(
                        is_existing=True,
                        message="Role mapping generated successfully",
                        status=ProcessingStatus.COMPLETED,
                    ).model_dump(mode="json")
                )
            
            if current_status == ProcessingStatus.FAILED:
                return RoleMappingBackgroundResponse(
                    is_existing=False,
                    status=ProcessingStatus.FAILED, 
                    message="Role mapping generation failed. Please try again later."
                )
        
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
            "is_existing": False,
            "message": "Role mapping generation started in background.",
            "status": ProcessingStatus.IN_PROGRESS
        }
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.exception("Role mapping processing failed to initiate")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initiate role mapping generation. Please try again later."
        )
