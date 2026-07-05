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

    async def fetch_hybrid_search_courses(
        self,
        keyword_emb: List[float],
        description_emb: List[float],
        combined_emb: List[float],
        limit: int = 80,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid weighted vector search across three embedding columns in
        course_metadata_weightage.

        Weightage: keywords 40%, description 20%, combined 40%.
        Returns rows sorted by weighted_score DESC.
        """
        sql_query = text(f"""
            SELECT
                identifier,
                name,
                (
                    0.40 * (1.0 - (keywords_embedding    <=> '{keyword_emb}')) +
                    0.20 * (1.0 - (description_embedding <=> '{description_emb}')) +
                    0.40 * (1.0 - (combined_embedding    <=> '{combined_emb}'))
                ) AS weighted_score
            FROM public.course_metadata_weightage
            ORDER BY weighted_score DESC
            LIMIT {limit};
        """)
        async with sessionmanager.session() as db:
            result = await db.execute(sql_query)
            return result.all()

    async def fetch_keyword_search_courses(
        self,
        keywords: List[str],
        limit: int = 40,
    ) -> List[Dict[str, Any]]:
        """
        Full-text + array keyword search on course_metadata_weightage.

        Strategy (OR-combined, ranked by match count):
          1. keywords[] array overlap  — GIN index hit
          2. name trigram match        — pg_trgm ilike
          3. description FTS           — to_tsvector / plainto_tsquery

        Returns (identifier, name, keyword_score) sorted DESC.
        keyword_score = number of distinct match signals (1-3).
        """
        if not keywords:
            return []

        # Build per-keyword conditions
        array_overlaps = " OR ".join(
            f"keywords && ARRAY[:{f'kw{i}'}]" for i, _ in enumerate(keywords)
        )
        name_ilike = " OR ".join(
            f"name ILIKE :{f'nl{i}'}" for i, _ in enumerate(keywords)
        )
        # plainto_tsquery handles multi-word phrases safely (no operator syntax needed)
        fts_parts = " OR ".join(
            f"to_tsvector('english', COALESCE(description, '')) @@ plainto_tsquery('english', :{f'fts{i}'})"
            for i, _ in enumerate(keywords)
        )

        params: Dict[str, Any] = {}
        for i, kw in enumerate(keywords):
            params[f"kw{i}"] = kw          # plain str — asyncpg wraps it in ARRAY on the SQL side
            params[f"nl{i}"] = f"%{kw}%"
            params[f"fts{i}"] = kw

        sql = text(f"""
            SELECT
                identifier,
                name,
                (
                    CASE WHEN ({array_overlaps}) THEN 1 ELSE 0 END +
                    CASE WHEN ({name_ilike})     THEN 1 ELSE 0 END +
                    CASE WHEN ({fts_parts})       THEN 1 ELSE 0 END
                )::float AS keyword_score
            FROM public.course_metadata_weightage
            WHERE
                ({array_overlaps})
                OR ({name_ilike})
                OR ({fts_parts})
            ORDER BY keyword_score DESC
            LIMIT {limit};
        """)

        async with sessionmanager.session() as db:
            result = await db.execute(sql, params)
            return result.all()

    async def fetch_vector_search_courses(self, embedding_values: List[float], limit: int = 60) -> List[Dict[str, Any]]:
        """Legacy single-embedding vector search (course_metadata_v3). Kept for fallback."""
        sql_query = text(f"""
        SELECT name, identifier,
               MAX(1.0 - (embedding <=> '{embedding_values}')) AS distance
        FROM public.course_metadata_v3
        GROUP BY name, identifier
        ORDER BY distance DESC LIMIT {limit};
        """)
        async with sessionmanager.session() as db:
            result = await db.execute(sql_query)
            return result.all()

    async def fetch_competency_typed_courses(
        self,
        combined_emb: List[float],
        competency_type: str,
        limit: int = 40,
    ) -> List[Dict[str, Any]]:
        """
        Vector search on combined_embedding pre-filtered to courses whose competencies_v6
        contains at least one entry with competencyAreaName matching competency_type.

        This ensures functional and behavioral courses reach the candidate pool even when
        the general hybrid search ranks domain courses higher.

        Args:
            combined_emb: Query vector (use competency_query embedding for best alignment).
            competency_type: "functional" or "behavioural" (matched case-insensitively).
            limit: Max rows to return.

        Returns:
            List of (identifier, name, score) rows.
        """
        sql_query = text(f"""
            SELECT
                identifier,
                name,
                (1.0 - (combined_embedding <=> '{combined_emb}')) AS score
            FROM public.course_metadata_weightage
            WHERE EXISTS (
                SELECT 1
                FROM jsonb_array_elements(competencies_v6) AS comp
                WHERE lower(comp->>'competencyAreaName') LIKE '%{competency_type}%'
            )
            ORDER BY score DESC
            LIMIT {limit};
        """)
        async with sessionmanager.session() as db:
            result = await db.execute(sql_query)
            return result.all()

    async def fetch_course_metadata(self, identifiers_str: str) -> Dict[str, Dict[str, Any]]:
        """Fetches competencies, duration, organisation, and description for a list of course identifiers."""
        competencies_query = text(f"""
            SELECT identifier, competencies_v6, duration, organisation, keywords, description, name
            FROM public.course_metadata_weightage
            WHERE identifier IN ({identifiers_str});
            """)
        async with sessionmanager.session() as db:
            competencies_result = await db.execute(competencies_query)
            return competencies_result.all()
        
# Initialize the CRUD utility for use across the application
crud_recommended_course = CRUDRecommendedCourse()