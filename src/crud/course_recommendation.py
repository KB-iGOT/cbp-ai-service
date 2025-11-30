import json
import uuid
from typing import Optional, List, Dict, Any, Literal
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select


from ..core.database import sessionmanager 

from ..models.course_recommendation import RecommendationStatus, RecommendedCourse 


class CRUDRecommendedCourse:
    """
    CRUD methods for the RecommendedCourse model, supporting asynchronous operations.
    
    The public methods here are refactored to manage their own database session 
    lifecycle for use in self-contained background tasks (e.g., Celery/RQ workers).
    """
    
    # --- Helper method to perform lookup within an already open session ---
    async def _get_by_id_in_session(self, db: AsyncSession, recommendation_id: uuid.UUID) -> Optional[RecommendedCourse]:
        """Internal method to retrieve a record using an injected session."""
        stmt = select(RecommendedCourse).filter(RecommendedCourse.id == recommendation_id)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_by_id(self, recommendation_id: uuid.UUID) -> Optional[RecommendedCourse]:
        """
        Retrieves a RecommendedCourse record by its primary key ID, managing its own session.
        (Corresponds to step 1 in the background task if executed standalone).
        """
        async with sessionmanager.session() as db:
            return await self._get_by_id_in_session(db, recommendation_id)

    async def get_by_role_mapping_id(
        self, 
        db: AsyncSession, 
        role_mapping_id: uuid.UUID, 
        user_id: uuid.UUID # Added user_id filter
    ) -> Optional[RecommendedCourse]:
        """
        Retrieves the first RecommendedCourse record associated with a specific 
        role mapping ID and user ID.
        
        Args:
            db: The async database session from FastAPI dependency.
            role_mapping_id: The ID of the RoleMapping to filter by.
            user_id: The ID of the user creating the recommendation.
            
        Returns:
            The first matching RecommendedCourse object, or None.
        """
        stmt = select(RecommendedCourse).filter(
            RecommendedCourse.role_mapping_id == role_mapping_id,
            RecommendedCourse.user_id == user_id # Apply user_id filter
        ).limit(1)
        
        result = await db.execute(stmt)
        return result.scalars().first()

    async def delete_by_id(self, db: AsyncSession, recommendation_id: uuid.UUID) -> bool:
        """
        Deletes a RecommendedCourse record by its primary key ID.

        Args:
            db: The async database session.
            recommendation_id: The ID of the record to delete.
            
        Returns:
            True if the record was found and deleted, False otherwise.
        """
        recommendation_record = await self._get_by_id_in_session(db, recommendation_id)
        if recommendation_record:
            await db.delete(recommendation_record)
            await db.commit()
            return True
        return False
    
    async def create(
        self, 
        db: AsyncSession, 
        user_id: uuid.UUID, 
        role_mapping_id: uuid.UUID, 
        status: RecommendationStatus = "IN_PROGRESS"
    ) -> RecommendedCourse:
        """
        Creates a new RecommendedCourse record with initial placeholder data.
        
        Args:
            db: The async database session.
            user_id: The ID of the user creating the recommendation.
            role_mapping_id: The ID of the role mapping this recommendation belongs to.
            status: The initial status (defaults to IN_PROGRESS).
            
        Returns:
            The newly created RecommendedCourse object.
        """
        new_recommendation = RecommendedCourse(
            user_id=user_id,
            role_mapping_id=role_mapping_id,
            status=status,
            vector_query="",
            actual_courses=[],
            filtered_courses=[]
        )

        db.add(new_recommendation)
        # Note: commit/refresh are handled by the calling router/service layer 
        # for transactional control, but we'll include them here for completeness 
        # as a self-contained unit.
        await db.commit()
        await db.refresh(new_recommendation)
        
        return new_recommendation
    
    async def update_status_and_data(
        self, 
        recommendation_id: uuid.UUID, # Record ID is now used to fetch the record in the new session
        query_text: str, 
        embedding_values: List[float], 
        actual_courses: List[Dict[str, Any]], 
        final_filtered_courses: List[Dict[str, Any]]
    ) -> Optional[RecommendedCourse]:
        """
        Updates the record with final results and sets status to COMPLETED, 
        managing its own session.
        """
        stmt = (
            update(RecommendedCourse)
            .where(RecommendedCourse.id == recommendation_id)
            .values(
                vector_query=query_text,
                embedding=embedding_values,
                actual_courses=actual_courses,
                filtered_courses=final_filtered_courses,
                status = "COMPLETED" 

            )
            .returning(RecommendedCourse)
        )
        async with sessionmanager.session() as db:
            result = await db.execute(stmt)
            await db.commit()
            updated_record = result.scalar_one()
            return updated_record

    async def update_status_to_failed(
        self, 
        recommendation_id: uuid.UUID, 
        error_message: str
    ) -> Optional[RecommendedCourse]:
        """
        Updates the record status to FAILED after an exception, managing its own session.
        """
        stmt = (
            update(RecommendedCourse)
            .where(RecommendedCourse.id == recommendation_id)
            .values(
                status = "FAILED",
                error_message = error_message
            )
            .returning(RecommendedCourse)
        )
        async with sessionmanager.session() as db:
            result = await db.execute(stmt)
            await db.commit()
            updated_record = result.scalar_one()
            return updated_record

    async def fetch_vector_search_courses(self, embedding_values: List[float]) -> List[Dict[str, Any]]:
        """
        Executes the raw SQL query against the database using vector similarity 
        and hardcoded filters, managing its own session.
        
        Args:
            embedding_values: The list of floats representing the query vector.
            
        Returns:
            A list of dictionaries containing course name, identifier, and distance.
        """

        sql_query = text(f"""
        (SELECT name, identifier,
        MAX(1.0 - (embedding <=> '{embedding_values}'))
        AS distance FROM public.course_metadata_v2
        GROUP BY name, identifier
        ORDER BY distance DESC LIMIT 20)
        UNION
        SELECT name, identifier, 0 AS distance FROM public.course_metadata_v2 WHERE name LIKE '%Communication%'
        UNION
        SELECT name, identifier, 0 AS distance FROM public.course_metadata_v2 WHERE name LIKE '%GenAI%'
        """)

        async with sessionmanager.session() as db:
            result = await db.execute(sql_query)
            return result.all()

    async def fetch_course_metadata(self, identifiers_str: str) -> Dict[str, Dict[str, Any]]:
        """
        Fetches competencies, duration, and organization for a list of course identifiers
        using a raw SQL query and manages its own session.
        
        Args:
            identifiers_str: identifiers (str) to look up.
            
        Returns:
            A dictionary mapped by identifier to a dictionary of its metadata.
        """
        
        # Execute the raw SQL query
        competencies_query = text(f"""
            SELECT identifier, competencies_v6, duration, organisation FROM public.course_metadata_v2
            WHERE identifier IN ({identifiers_str});
            """)
        
        async with sessionmanager.session() as db:
            competencies_result = await db.execute(competencies_query)
            return competencies_result.all()
        
# Initialize the CRUD utility for use across the application
crud_recommended_course = CRUDRecommendedCourse()