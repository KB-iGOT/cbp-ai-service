import json
import os
import uuid
from typing import Optional, List, Dict, Any, Literal
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select


from ..core.database import sessionmanager
from ..core.logger import logger

from ..models.course_recommendation import RecommendationStatus, RecommendedCourse


def _flag(name: str, default: bool) -> bool:
    """Read a boolean feature flag from the environment."""
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- retrieval feature flags (override via env vars) ------------------------
# Phase 1 core: keyword (lexical) + semantic (dense) hybrid.
HYBRID_SEARCH_ENABLED    = _flag("HYBRID_SEARCH_ENABLED", True)
# Business rule: gently prefer Communication / GenAI courses among relevant ones.
COMM_GENAI_BOOST_ENABLED = _flag("COMM_GENAI_BOOST_ENABLED", True)
# On by default: removing it scored clearly worst in LLM-judge A/B (it nets helpful).
COMPETENCY_BOOST_ENABLED = _flag("COMPETENCY_BOOST_ENABLED", True)
# Phase 2 (ON by default; opt out with CONTENT_RERANK_ENABLED=false): re-rank
# hybrid candidates against course CONTENT embeddings before the LLM stage.
# See content_rerank() below.
#
# REQUIRES a `public.content_embeddings` table that is NOT created by the app's
# migrations -- it must be restored / populated separately (e.g. from the
# content-embeddings dump or your content-embedding pipeline). Minimum schema:
#     path        text         -- "<course_id>" or "<course_id>/<resource_id>#chunkN"
#     embedding   vector(768)  -- same model/space as course_metadata_v2.embedding
# Join key to course_metadata_v2.identifier is split_part(path,'/',1) (the parent
# course id) -- NOT the `identifier` column, which holds the per-chunk/resource id.
# If the table is MISSING, content_rerank() logs a warning and falls back to the
# hybrid ordering on every request -- the app stays runnable, but set this flag to
# false in environments that don't have the table to avoid the per-request warning.
CONTENT_RERANK_ENABLED   = _flag("CONTENT_RERANK_ENABLED", True)
CONTENT_RERANK_TOP_N     = int(os.getenv("CONTENT_RERANK_TOP_N", "40"))      # narrow to this many before the LLM
CONTENT_RERANK_WEIGHT    = float(os.getenv("CONTENT_RERANK_WEIGHT", "0.5"))  # blend weight: content vs hybrid score


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

    async def fetch_vector_search_courses(
        self,
        embedding_values: List[float],
        query_text: str = "",
        themes: List[str] = None,
        sub_themes: List[str] = None,
        candidate_pool: int = 60,
        fusion_depth: int = 150,
    ) -> List[Dict[str, Any]]:
        """
        Executes the raw SQL query against the database using vector similarity,
        dynamic competency overlap boosting, and hardcoded keyword boosts, 
        managing its own session.
        
        Args:
            embedding_values: The list of floats representing the query vector.
            themes: List of competency themes associated with the designation.
            sub_themes: List of competency sub-themes associated with the designation.
            
        Returns:
            A list of tuples containing course name, identifier, and distance (combined score).
        """
        # Guard against empty themes/sub_themes lists by passing a list with an empty string
        themes_param = themes if themes else [""]
        sub_themes_param = sub_themes if sub_themes else [""]

        # Assemble the ranking SQL from feature flags. The metadata core is a
        # keyword(lexical) + semantic(dense) hybrid fused with Reciprocal Rank
        # Fusion; optional competency / Communication-GenAI boosts nudge ordering.
        # 'distance' is ALWAYS reported as raw cosine similarity (for cross-run
        # comparison), independent of how ordering is computed.
        if HYBRID_SEARCH_ENABLED:
            base_score = "f.rrf"               # RRF scale (~0 .. 0.033)
            comp_w, cg_w = 0.005, 0.01         # boosts scaled to RRF magnitude
            from_clause = """
        WITH dense AS (
            SELECT identifier,
                   ROW_NUMBER() OVER (ORDER BY embedding <=> :embedding) AS rnk
            FROM public.course_metadata_v2
            ORDER BY embedding <=> :embedding
            LIMIT :fusion_depth
        ),
        lexical AS (
            SELECT identifier,
                   ROW_NUMBER() OVER (
                       ORDER BY ts_rank_cd(
                           to_tsvector('english',
                               coalesce(name,'') || ' ' || coalesce(description,'') || ' ' ||
                               array_to_string(coalesce(keywords, ARRAY[]::text[]), ' ')),
                           websearch_to_tsquery('english', :qtext)) DESC
                   ) AS rnk
            FROM public.course_metadata_v2
            WHERE :qtext <> ''
              AND to_tsvector('english',
                      coalesce(name,'') || ' ' || coalesce(description,'') || ' ' ||
                      array_to_string(coalesce(keywords, ARRAY[]::text[]), ' ')
                  ) @@ websearch_to_tsquery('english', :qtext)
            LIMIT :fusion_depth
        ),
        fused AS (
            SELECT COALESCE(d.identifier, l.identifier) AS identifier,
                   COALESCE(1.0/(60 + d.rnk), 0.0) + COALESCE(1.0/(60 + l.rnk), 0.0) AS rrf
            FROM dense d
            FULL OUTER JOIN lexical l ON d.identifier = l.identifier
        )
        SELECT c.name, c.identifier, (1.0 - (c.embedding <=> :embedding)) AS distance
        FROM fused f
        JOIN public.course_metadata_v2 c ON c.identifier = f.identifier"""
        else:
            base_score = "(1.0 - (c.embedding <=> :embedding))"   # pure cosine (0 .. 1)
            comp_w, cg_w = 0.05, 0.05                              # boosts on cosine scale
            from_clause = """
        SELECT c.name, c.identifier, (1.0 - (c.embedding <=> :embedding)) AS distance
        FROM public.course_metadata_v2 c"""

        score_terms = [base_score]
        if COMPETENCY_BOOST_ENABLED:
            score_terms.append(f"""{comp_w} * COALESCE((
                SELECT COUNT(*) FROM jsonb_to_recordset(c.competencies_v6)
                     AS cv("competencyThemeName" text, "competencySubThemeName" text)
                WHERE cv."competencyThemeName" = ANY(:themes) OR cv."competencySubThemeName" = ANY(:sub_themes)
            ), 0)""")
        if COMM_GENAI_BOOST_ENABLED:
            score_terms.append(f"""{cg_w} * (CASE WHEN c.name ILIKE '%Communication%' OR c.name ILIKE '%GenAI%' THEN 1.0 ELSE 0.0 END)""")
        order_expr = " + ".join(score_terms)

        sql_query = text(f"""{from_clause}
        ORDER BY ({order_expr}) DESC
        LIMIT :limit;
        """)

        async with sessionmanager.session() as db:
            result = await db.execute(sql_query, {
                "embedding": str(embedding_values),
                "qtext": query_text or "",
                "themes": themes_param,
                "sub_themes": sub_themes_param,
                "limit": candidate_pool,
                "fusion_depth": fusion_depth,
            })
            return result.all()

    async def content_rerank(
        self,
        courses: List[Dict[str, Any]],
        embedding_values: List[float],
        top_n: int = CONTENT_RERANK_TOP_N,
        weight: float = CONTENT_RERANK_WEIGHT,
    ) -> List[Dict[str, Any]]:
        """
        Phase-2 middle layer (between hybrid search and the LLM): re-rank the
        hybrid candidates against COURSE CONTENT embeddings
        (public.content_embeddings), then narrow to top_n.

        JOIN KEY = the parent course id derived from content_embeddings.path,
        NOT the `identifier` column. content_embeddings is chunked: a row's `path`
        is either "<course_id>" (course-level) or "<course_id>/<resource_id>#chunkN"
        (resource-level), so the parent course id is always split_part(path,'/',1).
        Joining on `identifier` would match only the course-level rows and miss
        every resource chunk (the bulk of the content).

        For each candidate course we take the MAX cosine similarity over ALL its
        content chunks (summary + resources), then blend with the hybrid score:
            score = (1 - weight) * hybrid_cosine + weight * content_cosine
        Courses with no content keep their hybrid score (no penalty), so partial
        coverage never silently drops a course. Falls back to the original order
        (truncated) if the table is unavailable.

        Perf note: filtering on split_part(path,'/',1) scans the table; for a large
        content_embeddings, add a functional index:
            CREATE INDEX ON public.content_embeddings (split_part(path, '/', 1));
        """
        if not courses:
            return courses
        identifiers = [c["identifier"] for c in courses]
        sql = text("""
            SELECT split_part(path, '/', 1) AS course_id,
                   MAX(1.0 - (embedding <=> :embedding)) AS content_sim
            FROM public.content_embeddings
            WHERE split_part(path, '/', 1) = ANY(:ids) AND embedding IS NOT NULL
            GROUP BY split_part(path, '/', 1)
        """)
        try:
            async with sessionmanager.session() as db:
                res = await db.execute(sql, {"embedding": str(embedding_values), "ids": identifiers})
                content_sim = {row[0]: float(row[1]) for row in res.all()}
        except Exception as e:
            logger.warning(f"content_rerank skipped (content_embeddings unavailable?): {e}")
            return courses[:top_n]

        covered = 0
        for c in courses:
            cs = content_sim.get(c["identifier"])
            base = float(c.get("distance") or 0.0)
            if cs is not None:
                covered += 1
                c["content_sim"] = cs
                c["rerank_score"] = (1.0 - weight) * base + weight * cs
            else:
                c["content_sim"] = None
                c["rerank_score"] = base
        courses.sort(key=lambda c: c["rerank_score"], reverse=True)
        logger.info(
            f"content_rerank: {covered}/{len(courses)} candidates had content embeddings; "
            f"narrowed to top {top_n}"
        )
        return courses[:top_n]

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