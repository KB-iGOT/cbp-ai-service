import httpx
import uuid
from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...schemas.cbp_plan import CBPPlanSaveRequest, CBPPlanSaveResponse, CBPPlanUpdateRequest
from ...models.user import User

from ...crud.cbp_plan import crud_cbp_plan
from ...crud.course_recommendation import crud_recommended_course
from ...crud.user_added_course import crud_user_added_course
from ...crud.role_mapping import crud_role_mapping

from ...api.dependencies import get_current_active_user
from ...core.database import get_db_session
from ...core.configs import settings
from ...core.logger import logger

from ...utils.common import convert_for_json

router = APIRouter(tags=["CBP Plans"])

# CBP Plans APIs
async def search_courses(identifiers: List[str]) -> List[Dict[str, Any]]:
    if not identifiers:
        return []

    payload = {
        "request": {
            "filters": {
                "primaryCategory": ["Course"],
                "status": ["Live"],
                "courseCategory": ["Course"],
                "identifier": identifiers
            },
            "fields": [
                "name", "identifier", "description", "keywords",
                "organisation", "competencies_v6", "language", "duration"
            ],
            "sortBy": {"createdOn": "Desc"},
            "limit": 100
        }
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.KB_BASE_URL}/api/content/v1/search",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.KB_AUTH_TOKEN}"
            }
        )
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("content", [])

@router.post("/cbp-plan/save", response_model=CBPPlanSaveResponse)
async def save_cbp_plan(
    request: CBPPlanSaveRequest,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Save CBP (Competency-Based Program) plan with selected courses
    
    This endpoint:
    1. Fetches latest recommendation for the role mapping
    2. Extracts selected courses from filtered_courses based on course_identifiers
    3. Creates CBP plan with selected course details
    5. Stores in the table
    
    Args:
        request: CBP plan save request with role_mapping_id and course_identifiers
        
    Returns:
        CBP plan save confirmation with complete details
    """
    try:
        logger.info(f"Saving CBP plan for role mapping: {request.role_mapping_id} with {len(request.course_identifiers)} courses")
        
        # Validate role mapping exists
        role_mapping = await crud_role_mapping.get_by_id_and_user(db, request.role_mapping_id, current_user.user_id)
        
        if not role_mapping:
            logger.warning(f"Role mapping with ID {request.role_mapping_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found"
            )
        
        # Validate course identifiers are provided
        if not request.course_identifiers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one course identifier must be provided"
            )
        
        # Fetch latest recommendation for this role mapping
        latest_recommendation = await crud_recommended_course.get_by_role_mapping_id(db, request.role_mapping_id, current_user.user_id)
        
        if not latest_recommendation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No course recommendations found for this role mapping. Please generate recommendations first."
            )
        
        # Extract selected courses from filtered_courses
        filtered_courses = latest_recommendation.filtered_courses or []
        selected_courses = []
        fetched_courses = []

        filtered_lookup = {str(course.get("identifier")): course for course in filtered_courses if isinstance(course, dict)}

        for course_id in request.course_identifiers:
            course_str = str(course_id)
            if course_str in filtered_lookup:
                selected_courses.append(filtered_lookup[course_str])
                fetched_courses.append(course_str)
            else:
                logger.warning(f"Course ID {course_id} not found in filtered recommendations")
        
        # if not selected_courses:
        #     raise HTTPException(
        #         status_code=status.HTTP_400_BAD_REQUEST,
        #         detail="None of the provided course identifiers were found in the latest recommendations"
        #     )
        
        remaining_course = [c for c in request.course_identifiers if str(c) not in fetched_courses]
        
        courses_data = []
        # Use an async HTTP client to make the request
        if remaining_course:
            courses_data = await search_courses(remaining_course)
        
        remaining_course_uuid = [c for c in remaining_course if not str(c).startswith("do_")]
        user_courses = []
        if remaining_course_uuid:
            user_courses = await crud_user_added_course.get_user_added_courses_by_identifiers(
                db, 
                current_user.user_id, 
                request.role_mapping_id,
                remaining_course_uuid
            )

        # Convert ORM objects to dicts and UUIDs to strings for JSON
        user_courses_list = [
            {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in course.__dict__.items() if not k.startswith("_")}
            for course in user_courses
        ]

        selected_courses = convert_for_json(selected_courses)
        courses_data = convert_for_json(courses_data)
        user_courses_list = convert_for_json(user_courses_list)

        final_selected_courses = selected_courses + courses_data + user_courses_list

        # Create CBP plan record
        cbp_plan = await crud_cbp_plan.create(
            db,
            user_id=current_user.user_id,
            role_mapping_id=request.role_mapping_id,
            recommended_course_id=request.recommended_course_id,
            selected_courses=final_selected_courses
        )

        logger.info(f"CBP plan saved successfully with ID: {cbp_plan.id}, containing {len(selected_courses)} courses")
        return cbp_plan
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving CBP plan: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save CBP plan: {str(e)}"
        )

@router.get("/cbp-plan", response_model=CBPPlanSaveResponse)
async def get_cbp_plan(
    role_mapping_id: str = Query(..., description="Role Mapping ID to fetch CBP Plan"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Get CBP plan details by Role Mapping ID"""
    logger.info(f"Fetching CBP plan details for role mapping: {role_mapping_id}")
    try:
        
        existing_cbp_plan = await crud_cbp_plan.get_by_role_mapping(db, role_mapping_id, current_user.user_id)
        if not existing_cbp_plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No CBP Plan Details found for this role mapping. Please create a CBP Plan first."
            )
        logger.info(f"Successfully CBP Plan Details")
        return existing_cbp_plan
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting CBP plan: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get CBP plan")

@router.put("/cbp-plan/{cbp_plan_id}", response_model=CBPPlanSaveResponse)
async def update_cbp_plan(
    cbp_plan_id: str,
    request: CBPPlanUpdateRequest,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Update CBP plan details including courses, status, and metadata
    
    This endpoint allows updating:
    1. Selected courses (will re-extract from latest recommendations)
    
    Args:
        cbp_plan_id: CBP plan identifier
        request: Update request with fields to modify
        
    Returns:
        Updated CBP plan confirmation
    """
    try:
        logger.info(f"Updating CBP plan: {cbp_plan_id}")
        
        # Get existing CBP plan
        cbp_plan = await crud_cbp_plan.get_by_id(db,cbp_plan_id, current_user.user_id)
        if not cbp_plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="CBP plan not found"
            )
       
        # Update courses if new course identifiers provided
        if not request.course_identifiers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one course identifier must be provided"
            )
            
        # Fetch latest recommendation for this role mapping
        latest_recommendation = await crud_recommended_course.get_by_role_mapping_id(db, cbp_plan.role_mapping_id, current_user.user_id)
        
        if not latest_recommendation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No course recommendations found for this role mapping. Please generate recommendations first."
            )
        
        # Extract selected courses from filtered_courses
        filtered_courses = latest_recommendation.filtered_courses or []
        selected_courses = []
        fetched_courses = []

        filtered_lookup = {str(course.get("identifier")): course for course in filtered_courses if isinstance(course, dict)}

        for course_id in request.course_identifiers:
            course_str = str(course_id)
            if course_str in filtered_lookup:
                selected_courses.append(filtered_lookup[course_str])
                fetched_courses.append(course_str)
            else:
                logger.warning(f"Course ID {course_id} not found in filtered recommendations")
        
        # if not selected_courses:
        #     raise HTTPException(
        #         status_code=status.HTTP_400_BAD_REQUEST,
        #         detail="None of the provided course identifiers were found in the latest recommendations"
        #     )
        
        remaining_course = [c for c in request.course_identifiers if str(c) not in fetched_courses]
        
        courses_data = []
        # Use an async HTTP client to make the request
        if remaining_course:
            courses_data = await search_courses(remaining_course)
        
        remaining_course_uuid = [c for c in remaining_course if not str(c).startswith("do_")]
        user_courses = []
        if remaining_course_uuid:
            user_courses = await crud_user_added_course.get_user_added_courses_by_identifiers(
                db, 
                current_user.user_id, 
                cbp_plan.role_mapping_id,
                remaining_course_uuid
            )

        # Convert ORM objects to dicts and UUIDs to strings for JSON
        user_courses_list = [
            {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in course.__dict__.items() if not k.startswith("_")}
            for course in user_courses
        ]

        selected_courses = convert_for_json(selected_courses)
        courses_data = convert_for_json(courses_data)
        user_courses_list = convert_for_json(user_courses_list)

        final_selected_courses = selected_courses + courses_data + user_courses_list
        
        # Update courses and related fields
        update_records = {'selected_courses': final_selected_courses}
        cbp_plan = await crud_cbp_plan.update(db, cbp_plan_id, update_records)
        logger.info(f"CBP plan {cbp_plan_id} updated successfully.")
        return cbp_plan
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating CBP plan: {str(e)}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update CBP plan: {str(e)}"
        )

@router.delete("/cbp-plan/{cbp_plan_id}/course/{course_identifier}")
async def delete_course_from_cbp_plan(
    cbp_plan_id: uuid.UUID,
    course_identifier: str,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Delete an individual course from selected_courses in a CBP plan.

    Args:
        cbp_plan_id: UUID of the CBP plan
        course_identifier: Identifier of the course to remove (can be UUID or do_xxx format)

    Returns:
        Updated CBP plan details
    """
    try:
        logger.info(f"Deleting course '{course_identifier}' from CBP plan: {cbp_plan_id}")
        
        # Get existing CBP plan
        cbp_plan = await crud_cbp_plan.get_by_id(db, cbp_plan_id, current_user.user_id)
        
        if not cbp_plan:
            logger.warning(f"CBP plan with ID {cbp_plan_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="CBP plan not found or access denied"
            )
        
        # Get current selected courses
        selected_courses = cbp_plan.selected_courses or []
        
        if not selected_courses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No courses found in this CBP plan"
            )
        
        # Filter out the course to delete
        original_count = len(selected_courses)
        
        # Handle both UUID and string identifiers
        updated_courses = [
            course for course in selected_courses
            if str(course.get("identifier")) != str(course_identifier)
        ]
        
        new_count = len(updated_courses)
        
        # Check if course was found and removed
        if original_count == new_count:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Course with identifier '{course_identifier}' not found in CBP plan"
            )
        
        # # Check if at least one course remains
        # if new_count == 0:
        #     raise HTTPException(
        #         status_code=status.HTTP_400_BAD_REQUEST,
        #         detail="Cannot delete the last course. CBP plan must have at least one course."
        #     )
        
        # Update CBP plan with filtered courses
        update_records = {'selected_courses': updated_courses}
        updated_cbp_plan = await crud_cbp_plan.update(db, cbp_plan_id, update_records)
        
        logger.info(f"Successfully deleted course '{course_identifier}' from CBP plan {cbp_plan_id}. Remaining courses: {new_count}")
        
        return {
            "message": f"Successfully deleted course '{course_identifier}' from CBP plan",
            "cbp_plan_id": str(cbp_plan_id),
            "deleted_course_identifier": course_identifier,
            "remaining_courses": new_count
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting course from CBP plan: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete course from CBP plan: {str(e)}"
        )