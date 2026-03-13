"""
Service for validating designation eligibility for approval requests
"""
import uuid
import re
from typing import List, Dict, Tuple, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from ..models.role_mapping import RoleMapping
from ..models.course_recommendation import RecommendedCourse, RecommendationStatus
from ..core.logger import logger


class DesignationValidationService:
    """Service for validating designation eligibility"""
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def validate_designation_eligibility(
        self,
        designation_id: uuid.UUID
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Validate if a designation is eligible for approval request.
        
        Checks:
        1. Record exists
        2. course_recommendation is not null (has recommended_courses)
        3. course_recommendation_status == "COMPLETED" (saved)
        
        Args:
            designation_id: UUID of the designation (role_mapping)
            
        Returns:
            Tuple of (is_eligible, designation_name, reason)
            - is_eligible: True if all checks pass
            - designation_name: Name of the designation (or None if not found)
            - reason: Reason for ineligibility (or None if eligible)
        """
        try:
            # Query role_mapping with eager loading of recommended_courses
            stmt = (
                select(RoleMapping)
                .options(selectinload(RoleMapping.recommended_courses))
                .where(RoleMapping.id == designation_id)
            )
            
            result = await self.db.execute(stmt)
            role_mapping = result.scalars().first()
            
            # Check 1: Record exists
            if not role_mapping:
                logger.warning(f"Designation {designation_id} not found")
                return False, None, "Designation not found"
            
            designation_name = role_mapping.designation_name or "Unknown"
            
            # Check 2: Has course recommendations
            if not role_mapping.recommended_courses or len(role_mapping.recommended_courses) == 0:
                logger.info(f"Designation {designation_id} has no course recommendations")
                return False, designation_name, "Course recommendation not generated"
            
            # Check 3: Course recommendation status is COMPLETED (saved)
            has_completed = any(
                rc.status == RecommendationStatus.COMPLETED.value 
                for rc in role_mapping.recommended_courses
            )
            
            if not has_completed:
                logger.info(f"Designation {designation_id} has no completed course recommendations")
                return False, designation_name, "Course recommendation not saved"
            
            # All checks passed
            logger.debug(f"Designation {designation_id} is eligible")
            return True, designation_name, None
            
        except Exception as e:
            logger.error(f"Error validating designation {designation_id}: {str(e)}")
            return False, None, f"Validation error: {str(e)}"
    
    async def validate_multiple_designations(
        self,
        designation_ids: List[uuid.UUID]
    ) -> List[Dict]:
        """
        Validate multiple designations for eligibility.
        
        Args:
            designation_ids: List of designation UUIDs
            
        Returns:
            List of dicts with keys: id, name, is_eligible, reason
        """
        results = []
        
        for designation_id in designation_ids:
            is_eligible, name, reason = await self.validate_designation_eligibility(designation_id)
            
            results.append({
                "id": designation_id,
                "name": name or "Unknown",
                "is_eligible": is_eligible,
                "reason": reason
            })
        
        return results
    
    async def validate_and_collect_ineligible(
        self,
        designation_ids: List[uuid.UUID]
    ) -> List[Dict]:
        """
        Validate designations and return only ineligible ones.
        
        Args:
            designation_ids: List of designation UUIDs
            
        Returns:
            List of dicts with keys: id, reason (only for ineligible designations)
        """
        ineligible = []
        
        for designation_id in designation_ids:
            is_eligible, name, reason = await self.validate_designation_eligibility(designation_id)
            
            if not is_eligible:
                ineligible.append({
                    "id": designation_id,
                    "reason": reason or "Unknown reason"
                })
        
        return ineligible
    

# Initialize service instance (will be created per request with db session)
def get_validation_service(db: AsyncSession) -> DesignationValidationService:
    """Factory function to create validation service with db session"""
    return DesignationValidationService(db)
