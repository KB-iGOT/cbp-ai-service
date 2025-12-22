
from typing import List
import uuid
import httpx
from fastapi import APIRouter, Depends, HTTPException, status, Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...schemas.course_suggestion import CourseSuggestionRequest, CourseSuggestionRespose, CourseSuggestionSave, CourseSuggestionSaveResponse
from ...models.user import User
from ...api.dependencies import get_current_active_user
from ...core.database import get_db_session
from ...core.logger import logger
from ...core.configs import settings
from ...crud.course_suggestion import crud_suggested_course

router = APIRouter(tags=["Course Suggestions"])

# iGOT Course Suggestion APIs
@router.post("/course/suggestions", response_model=List[CourseSuggestionRespose])
async def fetch_course_suggestions(
    request: CourseSuggestionRequest,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user), 
):
    """
    fetch courses from iGOT platform
    Returns:
        Course suggestions with pagination info
    """
    logger.info(f"Fetching course suggestion requuest: {request.model_dump()}")
    try:
        # Prepare the payload for the external API call
        payload = {
            "request": {
                "filters": {
                    "primaryCategory": ["Course"],
                    "status": ["Live"],
                    "courseCategory": ["Course"]
                },
                "fields": ["name", "identifier", "description", "keywords", "organisation", "competencies_v6", "language", 'duration'],
                "sortBy": {"createdOn": "Desc"},
                "limit": request.limit,
                "offset": request.skip,
                "query": request.search_term
            }
        }
        
        # Use an async HTTP client to make the request
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.KB_BASE_URL}/api/content/v1/search",
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
            
            # Parse the JSON response
            data = response.json()
            courses_data = data.get("result", {}).get("content", [])
            logger.info("Fetched list of courses from iGOT platform")
            return courses_data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting course suggestions: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Failed to get course suggestions"
        )
    
@router.post("/course/suggestions/save", response_model=CourseSuggestionSaveResponse, status_code=status.HTTP_201_CREATED)
async def save_course_suggestions(
    request: CourseSuggestionSave, db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
    ):
    """
    Save course suggestions for a role mapping
    
    This endpoint allows users to suggest courses for specific role mappings.
    It handles both new suggestions and updates to existing suggestions.
    
    Args:
        suggestion: Course suggestion data with role_mapping_id and course_identifiers
        
    Returns:
        Created or updated course suggestion with full course details
    """
    try:
        logger.info(f"Saving course suggestions for role mapping: {request.role_mapping_id} with {len(request.course_identifiers)} courses")

        # Validate course identifiers are provided
        if not request.course_identifiers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one course identifier must be provided"
            )
            

        existing_suggested_course = await crud_suggested_course.get_by_role_mapping_and_user(db, request.role_mapping_id, current_user.user_id)

        if existing_suggested_course:
            logger.info(f"Updating existing suggestion with ID: {existing_suggested_course.id}")
            update_records = { 'course_identifiers':request.course_identifiers}
            existing_suggested_course = await crud_suggested_course.update(db, existing_suggested_course.id, update_records)
            logger.info(f"Successfully updated course suggestion with {len(request.course_identifiers)} courses")
            return existing_suggested_course
        else:
            logger.info("Creating new course suggestion")
            db_suggested_course = await crud_suggested_course.create(db, current_user.user_id,request.role_mapping_id, request.course_identifiers)
            
            logger.info(f"Successfully created course suggestion with ID: {db_suggested_course.id}")
            return db_suggested_course
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving course suggestions: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save course suggestions: {str(e)}"
        )
    
@router.get("/course/suggestions/{role_mapping_id}", response_model=List[CourseSuggestionRespose])
async def get_course_suggestions(
    role_mapping_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Get existing course suggestions for a role mapping"""
    try:
        suggestion = await crud_suggested_course.get_by_role_mapping_and_user(db, role_mapping_id,current_user.user_id)
        
        if not suggestion:
            return []
            # raise HTTPException(
            #     status_code=status.HTTP_404_NOT_FOUND,
            #     detail="No course suggestions found for this role mapping"
            # )
        
        if not suggestion.course_identifiers:
            return []
        
        # Prepare the payload for the external API call
        payload = {
            "request": {
                "filters": {
                    "primaryCategory": ["Course"],
                    "status": ["Live"],
                    "courseCategory": ["Course"],
                    "identifier": suggestion.course_identifiers # Use identifiers from the database
                },
                "fields": ["name", "identifier", "description", "keywords", "organisation", "competencies_v6", "language", 'duration'],
                "sortBy": {"createdOn": "Desc"},
                "limit": 1000 # Set a reasonable limit to fetch all courses if needed
            }
        }
        
        # Use an async HTTP client to make the request
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.KB_BASE_URL}/api/content/v1/search",
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
            
            # Parse the JSON response
            data = response.json()
            courses_data = data.get("result", {}).get("content", [])
            logger.info("Fetched existing course suggestions for a role mapping")
            return courses_data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting course suggestions: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Failed to get course suggestions"
        )

@router.delete("/course/suggestions/{role_mapping_id}")
async def delete_course_suggestions_by_role_mapping(
    role_mapping_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Delete all course suggestions for a specific role mapping.

    Args:
        role_mapping_id: UUID of the role mapping

    Returns:
        204 No Content on successful deletion
    """
    try:
        # Fetch the suggested course for this role mapping and user
        suggested_course = await crud_suggested_course.get_by_role_mapping_and_user(db, role_mapping_id,current_user.user_id)

        if not suggested_course:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No course suggestions found for this role mapping"
            )
        
        # Delete the suggested course entry
        await crud_suggested_course.delete_by_role_mapping_and_user(db, role_mapping_id,current_user.user_id)

        return {"detail": f"All course suggestions for role mapping '{role_mapping_id}' deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting course suggestions: {str(e)}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete course suggestions: {str(e)}"
        )

@router.delete("/course/suggestions/{role_mapping_id}/course/{course_identifier}")
async def delete_course_suggestion(
    role_mapping_id: uuid.UUID = Path(..., description="ID of the role mapping"),
    course_identifier: str = Path(..., description="Identifier of the course to delete"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Delete a single course from an existing course suggestion for a role mapping.

    Args:
        role_mapping_id: UUID of the role mapping
        course_identifier: Identifier of the course to remove

    Returns:
        204 No Content on successful deletion
    """
    try:
        # Fetch the existing suggested course
        suggested_course = await crud_suggested_course.get_by_role_mapping_and_user(db, role_mapping_id,current_user.user_id)

        if not suggested_course:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No course suggestions found for this role mapping"
            )
        
        if course_identifier not in suggested_course.course_identifiers:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Course identifier '{course_identifier}' not found in suggestions"
            )
        
        # Filter out the course to delete
        course_identifiers = [
            identifier for identifier in suggested_course.course_identifiers
            if identifier != course_identifier
        ]
        new_count = len(course_identifiers)
        update_records = {'course_identifiers': course_identifiers}
        await crud_suggested_course.update(db, suggested_course.id, update_records)
        
        # Optional: if no courses remain, you could delete the entire SuggestedCourse entry
        # if not suggested_course.course_identifiers:
        #     db.delete(suggested_course)
        #     db.commit()
        
        return {
            "message": f"Successfully deleted course '{course_identifier}' from suggestions",
            "role_mapping_id": str(role_mapping_id),
            "remaining_courses": new_count
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting course suggestion: {str(e)}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete course suggestion: {str(e)}"
        )