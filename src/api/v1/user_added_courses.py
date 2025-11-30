from typing import List
import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.user_added_course import UserAddedCourse
from ...models.user import User

from ...crud.role_mapping import crud_role_mapping
from ...crud.user_added_course import crud_user_added_course

from ...schemas.user_added_course import BulkDeleteResponse, CourseDeleteResponse, UserAddedCourseCreate, UserAddedCourseResponse, UserAddedCourseUpdate

from ...api.dependencies import get_current_active_user
from ...core.database import get_db_session
from ...core.logger import logger


router = APIRouter(tags=["User Added Courses"])

# User Added Courses APIs
@router.post("/user-added-courses", response_model=UserAddedCourseResponse, status_code=status.HTTP_201_CREATED)
async def create_user_added_course(
    course: UserAddedCourseCreate,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Add a new course from external sources for a specific role mapping
    
    This endpoint allows users to add courses from external platforms like Coursera, edX, Udemy, etc.
    The course will be associated with a specific role mapping for the current user.
    
    Args:
        course: Course details including name, platform, link, and optional metadata
        
    Returns:
        Details of the newly created user-added course
    """
    try:
        logger.info(f"Creating user-added course '{course.name}' for role mapping: {course.role_mapping_id}")
        
        # Validate role mapping exists and belongs to current user
        role_mapping = await crud_role_mapping.get_by_id_and_user(db, course.role_mapping_id, current_user.user_id)
        
        if not role_mapping:
            logger.warning(f"Role mapping with ID {course.role_mapping_id} not found or doesn't belong to user {current_user.username}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found or access denied"
            )
        course_dump = course.model_dump()
        # Create new user-added course
        db_course = UserAddedCourse(
            id=uuid.uuid4(),
            identifier=uuid.uuid4(),
            user_id=current_user.user_id,
            role_mapping_id=course.role_mapping_id,
            name=course.name,
            platform=course.platform,
            public_link=course.public_link,
            relevancy=course.relevancy,
            rationale=course.rationale,
            language=course.language,
            competencies=course_dump['competencies'] or []
        )
        
        db_course = await crud_user_added_course.create(db, db_course)
        
        logger.info(f"Successfully created user-added course with ID: {db_course.id}")
        return db_course
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating user-added course: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create user-added course: {str(e)}"
        )

@router.get("/user-added-courses/role-mapping/{role_mapping_id}", response_model=List[UserAddedCourseResponse])
async def get_user_added_courses_by_role_mapping(
    role_mapping_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get all user-added courses for a specific role mapping
    
    This endpoint retrieves all courses that the current user has added from external sources
    for a specific role mapping.
    
    Args:
        role_mapping_id: UUID of the role mapping
        
    Returns:
        List of user-added courses for the specified role mapping
    """
    try:
        logger.info(f"Fetching user-added courses for role mapping: {role_mapping_id}")
        
        # Validate role mapping exists and belongs to current user
        role_mapping = await crud_role_mapping.get_by_id_and_user(db, role_mapping_id, current_user.user_id)
        
        if not role_mapping:
            logger.warning(f"Role mapping with ID {role_mapping_id} not found or doesn't belong to user {current_user.username}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found or access denied"
            )
        
        # Get all user-added courses for this role mapping
        user_courses = await crud_user_added_course.get_courses_by_id_and_user(db, role_mapping_id, current_user.user_id)
        logger.info(f"Retrieved {len(user_courses)} user-added courses for role mapping {role_mapping.designation_name}")
        return user_courses
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching user-added courses by role mapping: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch user-added courses"
        )

@router.get("/user-added-courses/{course_id}", response_model=UserAddedCourseResponse)
async def get_user_added_course(
    course_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Get a specific user-added course by ID
    
    Args:
        course_id: UUID of the user-added course
        
    Returns:
        Details of the specified user-added course
    """
    try:
        logger.info(f"Fetching user-added course with ID: {course_id}")
        
        course = await crud_user_added_course.get_by_id(db, course_id, current_user.user_id)
        
        if not course:
            logger.warning(f"User-added course with ID {course_id} not found or doesn't belong to user {current_user.username}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User-added course not found or access denied"
            )
        
        logger.info(f"Retrieved user-added course: {course.name}")
        return course
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching user-added course: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch user-added course"
        )

@router.put("/user-added-courses/{course_id}", response_model=UserAddedCourseResponse)
async def update_user_added_course(
    course_id: uuid.UUID,
    course_update: UserAddedCourseUpdate,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Update a user-added course
    
    Args:
        course_id: UUID of the course to update
        course_update: Updated course data
        
    Returns:
        Updated course details
    """
    try:
        logger.info(f"Updating user-added course with ID: {course_id}")
        
        # Get existing course
        db_course = await crud_user_added_course.get_by_id(db, course_id, current_user.user_id)
        
        if not db_course:
            logger.warning(f"User-added course with ID {course_id} not found or doesn't belong to user {current_user.username}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User-added course not found or access denied"
            )
        
        # Update fields
        db_course = await crud_user_added_course.update(db, course_id, current_user.user_id, course_update)
        logger.info(f"Successfully updated user-added course: {db_course.name}")
        return db_course
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating user-added course: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user-added course"
        )

@router.delete("/user-added-courses/{course_id}", response_model=CourseDeleteResponse)
async def delete_user_added_course(
    course_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Delete a specific user-added course
    
    Args:
        course_id: UUID of the course to delete
        
    Returns:
        Deletion confirmation
    """
    try:
        logger.info(f"Deleting user-added course with ID: {course_id}")
        
        # Get existing course
        db_course = await crud_user_added_course.get_by_id(db, course_id, current_user.user_id)
        
        if not db_course:
            logger.warning(f"User-added course with ID {course_id} not found or doesn't belong to user {current_user.username}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User-added course not found or access denied"
            )
        
        course_name = db_course.name
        
        await crud_user_added_course.delete_by_id(db, course_id, current_user.user_id)
        
        logger.info(f"Successfully deleted user-added course: {course_name}")
        return CourseDeleteResponse(
            message=f"User-added course '{course_name}' deleted successfully",
            course_id=str(course_id)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting user-added course: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete user-added course"
        )

@router.delete("/user-added-courses/role-mapping/{role_mapping_id}", response_model=BulkDeleteResponse)
async def delete_all_user_added_courses_by_role_mapping(
    role_mapping_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Delete all user-added courses for a specific role mapping
    
    This endpoint removes all courses that the current user has added from external sources
    for a specific role mapping.
    
    Args:
        role_mapping_id: UUID of the role mapping
        
    Returns:
        Bulk deletion summary with count and details
    """
    try:
        logger.info(f"Deleting all user-added courses for role mapping: {role_mapping_id}")
        
        # Validate role mapping exists and belongs to current user
        role_mapping = await crud_role_mapping.get_by_id_and_user(db, role_mapping_id, current_user.user_id)
        
        if not role_mapping:
            logger.warning(f"Role mapping with ID {role_mapping_id} not found or doesn't belong to user {current_user.username}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found or access denied"
            )
        
        deleted_count = await crud_user_added_course.delete_all_by_role_mapping(db,role_mapping_id, current_user.user_id)
        
        if deleted_count == 0:
            logger.info(f"No user-added courses found for role mapping: {role_mapping.designation_name}")
            return BulkDeleteResponse(
                message=f"No user-added courses found for role mapping '{role_mapping.designation_name}'",
                deleted_count=0,
                role_mapping_id=str(role_mapping_id)
            )
        
        success_message = f"Successfully deleted {deleted_count} user-added courses for role mapping '{role_mapping.designation_name}'"
        
        logger.info(success_message)
        return BulkDeleteResponse(
            message=success_message,
            deleted_count=deleted_count,
            role_mapping_id=str(role_mapping_id)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting user-added courses by role mapping: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete user-added courses"
        )