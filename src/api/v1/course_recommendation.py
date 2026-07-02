import json
import os
import re
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from google import genai
from google.genai import types

from ...models.course_recommendation import RecommendationStatus
from ...models.user import User
from ...schemas.course_recommendation import RecommendCourseCreate, RecommendedCourseResponse

from ...core.database import get_db_session, sessionmanager
from ...core.logger import logger
from ...core.configs import settings

from ...crud.course_recommendation import crud_recommended_course
from ...crud.role_mapping import crud_role_mapping
from ...crud.course_suggestion import crud_suggested_course
from ...crud.user_added_course import crud_user_added_course

from ...api.dependencies import get_current_active_user

router = APIRouter(tags=["Course Recommendations"])

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS
client = genai.Client(
    project=settings.GOOGLE_PROJECT_ID,
    location="global",
    vertexai=True
)

embedding_client = genai.Client(
    api_key=settings.GOOGLE_API_KEY
)

# Curse Recommendation APIs
async def get_embedding(text: str) -> list:

    logger.info(f"Generating embedding for text '{text[:50]}...")

    if not text.strip():
        print("Warning: Attempted to get embedding for empty text. Returning empty list.")
        return []
    
    vector_query = f"task: search result | query: {text}"

    try:
        response = await embedding_client.aio.models.embed_content(
            model=settings.GOOGLE_EMBEDDING_MODEL,
            contents=vector_query,
            config=types.EmbedContentConfig(output_dimensionality=settings.EMBEDDING_OUTPUT_DIMENSIONALITY)
        )
        
        return response.embeddings
    except Exception as e:
        print(f"Error generating embedding for text '{text[:50]}...': {e}")
        return []

async def get_content_chunk_embedding(text_str: str) -> list:
    """
    Embeds text for comparison against public.content_embeddings, which is still in the
    older settings.CONTENT_CHUNK_EMBEDDING_MODEL space (unlike course_metadata_v3, which
    was migrated to the settings.GOOGLE_EMBEDDING_MODEL space get_embedding() uses above).
    """
    if not text_str.strip():
        return []
    try:
        response = await client.aio.models.embed_content(
            model=settings.CONTENT_CHUNK_EMBEDDING_MODEL,
            contents=text_str,
            config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY")
        )
        return response.embeddings[0].values
    except Exception as e:
        logger.warning(f"Error generating content-chunk embedding: {e}")
        return []


BLOCKLIST_PATTERNS = [
    r'^resource',
    r'^module',
    r'^overview',
    r'^introduction',
    r'^links',
    r'^list of',
    r'^परिचय$',
    r'^सीखने के उद्देश्य$',
    r'^व्यवस्था$',
]

HYPR4_RELEVANCY_THRESHOLD = 60
HYPR4_SIMILARITY_THRESHOLD = 0.0


def is_valid_course(name: str) -> bool:
    if not name:
        return False
    name_lower = name.strip().lower()
    for pattern in BLOCKLIST_PATTERNS:
        if re.match(pattern, name_lower):
            return False
    return True


def deduplicate_courses(courses: list) -> list:
    seen = {}
    for c in courses:
        org = c.get('organisation', '')
        dur = c.get('duration', '')
        key = (str(org), str(dur))
        if key in seen:
            if c['rrf_score'] > seen[key]['rrf_score']:
                seen[key] = c
        else:
            seen[key] = c
    return list(seen.values())


async def expand_query_for_hybrid_search(user_profile: str) -> dict:
    logger.info("Generating semantic summary and keywords for hybrid search...")
    system_instruction = """You are an expert search query generator for course catalogs.
    Analyze the user profile. Extract core concepts, roles, and competencies.
    Pay special attention to the Wing/Division/Section and Sector fields as they indicate
    the specific work allocation context — use them to focus the semantic summary on
    domain-relevant training needs.
    Return a JSON object with:
    1. 'semantic_summary': A rich 1-2 paragraph description of the role's essence (for vector search).
    2. 'search_keywords': A tsquery formatted string combining key terms using '|' (OR) or '&' (AND). Limit to 5-10 highly specific keywords. Avoid common stop words. Example: "leadership | python | management"
    """

    user_part = types.Part.from_text(text=user_profile)

    generate_content_config = types.GenerateContentConfig(
        temperature=0.2,
        response_mime_type="application/json",
        response_schema={
            "type": "OBJECT",
            "properties": {
                "semantic_summary": {"type": "STRING"},
                "search_keywords": {"type": "STRING"}
            },
            "required": ["semantic_summary", "search_keywords"]
        },
        system_instruction=[types.Part.from_text(text=system_instruction)],
    )

    response = await client.aio.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents=[types.Content(role="user", parts=[user_part])],
        config=generate_content_config,
    )
    return json.loads(response.text)

async def get_filtered_courses_by_llm(query, user_profile):
    
    logger.info("Filtering fetched courses by LLM")
    
    text1 = types.Part.from_text(text=f"""
    Analyze the following list of courses and provide a relevancy percentage for each, indicating how relevant you believe it is to the given to the given role. The role is described by the following:
    {user_profile}

    For each course, provide a 1-2 lines rationale explaining your assigned relevancy percentage. 

    ## SORT
    Sort the output in descending order of Relevancy.

    ## INPUT
    Here are the courses:
    {query}
    """)
    si_text1 = f"""
    You are an expert in analyzing professional development needs and recommending relevant training. 
    Your task is to assess the relevancy of various courses to a specific role and learning objective within a government administration context.
    You are responsible for the competencies of civil servants.
    """
    
    model = "gemini-3.5-flash"
    contents = [
        types.Content(
            role="user",
            parts=[
                text1
            ]
        )
    ]

    generate_content_config = types.GenerateContentConfig(
        temperature=0,
        top_p=1,
        seed=0,
        max_output_tokens=65535,
        safety_settings=[types.SafetySetting(
            category="HARM_CATEGORY_HATE_SPEECH",
            threshold="OFF"
        ), types.SafetySetting(
            category="HARM_CATEGORY_DANGEROUS_CONTENT",
            threshold="OFF"
        ), types.SafetySetting(
            category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
            threshold="OFF"
        ), types.SafetySetting(
            category="HARM_CATEGORY_HARASSMENT",
            threshold="OFF"
        )],
        response_mime_type="application/json",
        response_schema={ "type":"ARRAY", "items":{ "type":"OBJECT", "properties":{ "identifier":{ "type":"STRING", "description":"The ID of the course." }, "course":{ "type":"STRING", "description":"The name of the course." }, "relevancy":{ "type":"INTEGER", "description":"A percentage indicating the relevancy of the course, from 0 to 100." }, "rationale":{ "type":"STRING", "description":"The reasoning behind the relevancy score of the course." } }, "required":[ "course", "relevancy", "rationale" ] }, "description":"A list of courses with their relevancy and rationale for a specific context." },
        system_instruction=[types.Part.from_text(text=si_text1)],
        thinking_config=types.ThinkingConfig(
            include_thoughts=False,
            thinking_budget=-1,
        ),
    )

    response = await client.aio.models.generate_content(
        model=model,
        contents=contents,
        config=generate_content_config,
    )
    
    logger.info("Filtered courses successfully")
    return response.text

async def process_recommendation_task(recommendation_id: uuid.UUID, user_profile: str):
    """
    Background task running the HYPR4 hybrid search pipeline (metadata vector search
    -> keyword FTS -> content-chunk refinement -> org-level retrieval -> LLM verification).
    Manages its own DB session.
    """
    logger.info(f"Background task started for recommendation_id: {recommendation_id}")

    try:
        # 1. Retrieve the record to update
        rec_record = await crud_recommended_course.get_by_id(recommendation_id)
        if not rec_record:
            logger.error(f"Record {recommendation_id} not found in background task")
            return

        async with sessionmanager.session() as db:
            # 2. Fetch role mapping (needed for Layer 2.5 org-level retrieval)
            role_mapping = await crud_role_mapping.get_by_id_and_user(db, rec_record.role_mapping_id, rec_record.user_id)

            # 3. Expand query into a semantic summary + keyword search terms
            expanded = await expand_query_for_hybrid_search(user_profile)
            semantic_summary = expanded.get('semantic_summary', '')
            search_keywords_raw = expanded.get('search_keywords', '')
            clean_keywords = re.sub(r'[^a-zA-Z0-9\s]', ' ', search_keywords_raw)
            words = [w.strip() for w in clean_keywords.split() if w.strip()]
            search_keywords = ' | '.join(words)

            # 4. Generate embedding for the semantic summary
            embedding_list = await get_embedding(semantic_summary)
            if not embedding_list:
                raise Exception("Failed to generate embeddings")
            embedding_values = embedding_list[0].values

            # 4b. Separate embedding for content_embeddings (still 768-dim, older model)
            chunk_embedding_values = await get_content_chunk_embedding(semantic_summary)

            # ============================================================
            # LAYER 1: Course Metadata Search (coarse candidate retrieval)
            # ============================================================
            metadata_search_query = text(f"""
            SELECT name, identifier,
                (1.0 - (embedding <=> '{embedding_values}')) AS similarity
            FROM public.course_metadata_v3
            WHERE embedding IS NOT NULL
            ORDER BY similarity DESC
            LIMIT 150;
            """)
            metadata_result = await db.execute(metadata_search_query)
            metadata_courses = metadata_result.all()
            logger.info(f"Layer 1: Retrieved {len(metadata_courses)} candidate courses from course_metadata_v3")

            if HYPR4_SIMILARITY_THRESHOLD > 0.0:
                metadata_courses = [r for r in metadata_courses if r.similarity >= HYPR4_SIMILARITY_THRESHOLD]

            candidate_ids = [row.identifier for row in metadata_courses]
            candidate_ids_str = ", ".join(f"'{id}'" for id in candidate_ids)

            # ============================================================
            # LAYER 1b: Keyword FTS Search (merged into candidate pool)
            # ============================================================
            keyword_only_courses = []
            if search_keywords:
                try:
                    keyword_search_query = text(f"""
                        SELECT identifier, name, duration, organisation,
                            ts_rank_cd(search_vector, to_tsquery('english', '{search_keywords}')) AS keyword_score
                        FROM public.course_metadata_v3
                        WHERE search_vector @@ to_tsquery('english', '{search_keywords}')
                        ORDER BY keyword_score DESC
                        LIMIT 100;
                    """)
                    keyword_result = await db.execute(keyword_search_query)
                    keyword_courses = keyword_result.all()

                    vector_ids = {row.identifier for row in metadata_courses}
                    keyword_only_courses = [row for row in keyword_courses if row.identifier not in vector_ids]

                    candidate_ids.extend(row.identifier for row in keyword_only_courses)
                    candidate_ids_str = ", ".join(f"'{id}'" for id in candidate_ids)
                except Exception as e:
                    await db.rollback()
                    logger.warning(f"Layer 1b: Keyword search skipped — {e}")

            # ============================================================
            # LAYER 2: Content Embeddings Refinement (chunk-level scoring)
            # ============================================================
            refined_courses = []
            if candidate_ids_str and chunk_embedding_values:
                try:
                    refinement_query = text(f"""
                    SELECT
                        identifier AS course_id,
                        MIN(embedding <=> '{chunk_embedding_values}') AS best_chunk_distance,
                        (1.0 - MIN(embedding <=> '{chunk_embedding_values}')) AS chunk_similarity
                    FROM public.content_embeddings
                    WHERE embedding IS NOT NULL
                        AND identifier IN ({candidate_ids_str})
                    GROUP BY identifier
                    ORDER BY best_chunk_distance ASC
                    LIMIT 80;
                    """)
                    refinement_result = await db.execute(refinement_query)
                    refined_courses = refinement_result.all()

                    if HYPR4_SIMILARITY_THRESHOLD > 0.0:
                        refined_courses = [r for r in refined_courses if r.chunk_similarity >= HYPR4_SIMILARITY_THRESHOLD]
                except Exception as e:
                    await db.rollback()
                    logger.warning(f"Layer 2: Content chunk refinement skipped — {e}")
                    refined_courses = []

            refined_ids = {row.course_id for row in refined_courses}
            metadata_only_courses = [row for row in metadata_courses if row.identifier not in refined_ids]
            metadata_only_courses = sorted(metadata_only_courses, key=lambda r: r.similarity, reverse=True)[:20]

            all_course_ids = [row.course_id for row in refined_courses]
            all_course_ids.extend([row.identifier for row in metadata_only_courses])
            all_course_ids.extend([row.identifier for row in keyword_only_courses])
            all_ids_str = ", ".join(f"'{id}'" for id in all_course_ids)

            name_map = {}
            if all_ids_str:
                name_query = text(f"""
                    SELECT identifier, name, duration, organisation
                    FROM public.course_metadata_v3
                    WHERE identifier IN ({all_ids_str});
                """)
                name_result = await db.execute(name_query)
                name_map = {row.identifier: row for row in name_result.all()}

            combined_for_llm = []
            for row in refined_courses:
                meta = name_map.get(row.course_id)
                if not meta or not is_valid_course(meta.name):
                    continue
                combined_for_llm.append({
                    "identifier": row.course_id,
                    "name": meta.name,
                    "duration": meta.duration,
                    "organisation": meta.organisation,
                    "rrf_score": row.chunk_similarity
                })
            for row in metadata_only_courses:
                meta = name_map.get(row.identifier)
                if not meta or not is_valid_course(meta.name):
                    continue
                combined_for_llm.append({
                    "identifier": row.identifier,
                    "name": meta.name,
                    "duration": meta.duration,
                    "organisation": meta.organisation,
                    "rrf_score": row.similarity
                })

            if keyword_only_courses:
                captured_ids = {c['identifier'] for c in combined_for_llm}
                for row in keyword_only_courses:
                    if row.identifier in captured_ids:
                        continue
                    meta = name_map.get(row.identifier)
                    if not meta or not is_valid_course(meta.name):
                        continue
                    combined_for_llm.append({
                        "identifier": row.identifier,
                        "name": meta.name,
                        "duration": meta.duration,
                        "organisation": meta.organisation,
                        "rrf_score": float(row.keyword_score)
                    })

            combined_for_llm = deduplicate_courses(combined_for_llm)
            logger.info(f"After filtering & dedup: {len(combined_for_llm)} candidates for LLM")

            # ============================================================
            # LAYER 2.5: Organization-Level Course Retrieval
            # ============================================================
            org_courses = []
            if role_mapping:
                for lookup_name in filter(None, [role_mapping.state_center_name, role_mapping.department_name]):
                    try:
                        org_query = text(f"""
                            SELECT identifier, name, duration, organisation
                            FROM public.course_metadata_v3
                            WHERE organisation @> ARRAY['{lookup_name}']
                            LIMIT 50;
                        """)
                        org_result = await db.execute(org_query)
                        org_courses = org_result.all()
                        if org_courses:
                            break
                    except Exception as e:
                        await db.rollback()
                        logger.warning(f"Layer 2.5: Query failed for '{lookup_name}' — {e}")

            if org_courses:
                existing_ids = {c['identifier'] for c in combined_for_llm}
                for row in org_courses:
                    if row.identifier in existing_ids:
                        continue
                    if not is_valid_course(row.name):
                        continue
                    combined_for_llm.append({
                        "identifier": row.identifier,
                        "name": row.name,
                        "duration": row.duration,
                        "organisation": row.organisation,
                        "rrf_score": 1.0
                    })

            # ============================================================
            # LAYER 3: LLM Verification & Relevancy Scoring
            # ============================================================
            logger.info(f"Layer 3: LLM Verification ({len(combined_for_llm)} candidates)")

            relevant_courses_prompt = "\n".join(
                f"Course Name: {c['name']}, Course ID: {c['identifier']}" for c in combined_for_llm
            )

            filtered_courses_json = await get_filtered_courses_by_llm(relevant_courses_prompt, user_profile)
            filtered_courses = json.loads(filtered_courses_json)

            filtered_identifiers = [c.get('identifier') for c in filtered_courses if c.get('identifier')]
            competencies_map = {}
            if filtered_identifiers:
                identifiers_str = ", ".join(f"'{id}'" for id in filtered_identifiers)
                competencies_result = await crud_recommended_course.fetch_course_metadata(identifiers_str)
                competencies_map = {row.identifier: row for row in competencies_result}

            rrf_lookup = {c['identifier']: c['rrf_score'] for c in combined_for_llm}

            formatted_courses = []
            for course in filtered_courses:
                identifier = course.get('identifier')
                if not identifier:
                    continue

                competencies = None
                duration = None
                organisation = None
                if identifier in competencies_map:
                    data = competencies_map[identifier]
                    duration = data.duration
                    organisation = data.organisation
                    competencies = data.competencies_v6

                formatted_courses.append({
                    "identifier": identifier,
                    "course": course.get('course', ''),
                    "relevancy": course.get('relevancy', 0),
                    "rationale": course.get('rationale', ''),
                    "is_public": False,
                    "competencies": competencies,
                    "duration": duration,
                    "organisation": organisation,
                    "rrf_score": rrf_lookup.get(identifier, 0)
                })

            if HYPR4_RELEVANCY_THRESHOLD > 0:
                formatted_courses = [c for c in formatted_courses if c.get('relevancy', 0) >= HYPR4_RELEVANCY_THRESHOLD]

            logger.info(f"Final: {len(formatted_courses)} courses after LLM verification")

        # 5. Update DB Record to COMPLETED (only DB persistence — no CSV, ever)
        query_text = f"{semantic_summary}\n\nKeywords: {search_keywords}" if search_keywords else semantic_summary
        await crud_recommended_course.update_status_and_data(
            recommendation_id,
            query_text,
            embedding_values,
            combined_for_llm,
            formatted_courses,
        )

        # 6. Best-effort debug log (pure filesystem I/O, can never fail the request)
        try:
            os.makedirs("logs", exist_ok=True)
            output_file = f"logs/hypr4_results_{rec_record.role_mapping_id}.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(formatted_courses, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to write HYPR4 debug log: {e}")

        logger.info(f"Course Recommendation Background task completed successfully for {recommendation_id}")

    except Exception as e:
        logger.exception(f"Course Recommmendation Background task failed for {recommendation_id}:")
        # Update record to FAILED
        try:
            await crud_recommended_course.update_status_to_failed(recommendation_id, str(e))
        except Exception:
            logger.exception(f"CRITICAL: Failed to update status to FAILED for {recommendation_id}:")

@router.post("/course-recommendations/generate", response_model=RecommendedCourseResponse, status_code=status.HTTP_202_ACCEPTED)
async def generate_course_recommendations(
    request: RecommendCourseCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Generate Course Recommedation by role mapping ID"""
    try:
        role_mapping_id = request.role_mapping_id
        logger.info(f"Generating course recommendations for role mapping: {role_mapping_id} by user: {current_user.user_id}")
        
        # Get role mapping
        role_mapping = await crud_role_mapping.get_by_id_and_user(db, role_mapping_id, current_user.user_id)
        if not role_mapping:
            logger.warning(f"Role mapping with ID {role_mapping_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found"
            )
        
        existing_recommendation = await crud_recommended_course.get_by_role_mapping_id(db, role_mapping_id, current_user.user_id)
        if existing_recommendation:
            print(f"Found existing recommendation for Role mapping ID: {role_mapping_id}")
            current_status = existing_recommendation.status
            
            if current_status == RecommendationStatus.IN_PROGRESS:
                return existing_recommendation
            
            if current_status == RecommendationStatus.COMPLETED:
                response = RecommendedCourseResponse.model_validate(existing_recommendation)
                response.is_existing = True
                return JSONResponse(
                    status_code=status.HTTP_201_CREATED,
                    content=response.model_dump(mode="json")
                )
            
            if current_status == RecommendationStatus.FAILED:
                logger.info("Found failed records. Cleaning up to retry...")
                # Delete all records matching the filter to ensure a clean slate
                await db.delete(existing_recommendation)
                await db.commit()
        
        instruction_line = f"\n        Work Allocation Instructions: {role_mapping.instruction}" if role_mapping.instruction else ""
        user_profile = f"""
        Ministry/State Name: {role_mapping.state_center_name}
        Department Name: {role_mapping.department_name if role_mapping.department_name else 'N/A'}
        Sector: {role_mapping.sector_name or 'N/A'}
        Wing/Division/Section: {role_mapping.wing_division_section or 'N/A'}
        Designation Name: {role_mapping.designation_name}
        Roles & Responsibilities: {role_mapping.role_responsibilities}
        Activities: {role_mapping.activities}
        Competencies: {json.dumps(role_mapping.competencies, indent=2)}{instruction_line}
        """

        new_recommendation = await crud_recommended_course.create(
            db,
            current_user.user_id,
            role_mapping_id,
            RecommendationStatus.IN_PROGRESS
        )

        background_tasks.add_task(
            process_recommendation_task, 
            new_recommendation.id, 
            user_profile
        )

        logger.info(f"Initiated background generation for {new_recommendation.id}")
        return new_recommendation
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error initiating course recommendation:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiating course recommendations: {str(e)}"
        )

@router.get("/course-recommendations", response_model=RecommendedCourseResponse)
async def get_course_recommendations(
    role_mapping_id: str = Query(..., description="Role Mapping ID to fetch recommended courses"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Get Generated Course Recommedation by role mapping ID"""
    try:
        logger.info(f"Fetching recommended courses for role mapping: {role_mapping_id}")
        
        existing_recommendation = await crud_recommended_course.get_by_role_mapping_id(db, role_mapping_id, current_user.user_id)
        if not existing_recommendation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No course recommendations found for this role mapping. Please generate recommendations first."
            )
        logger.info("Successfully fetched course recommendations")
        return existing_recommendation
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in Fetching recommended courses endpoint: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch recommended courses: {str(e)}"
        )

@router.delete("/course-recommendations/role-mapping/{role_mapping_id}")
async def delete_course_recommendations_by_role_mapping(
    role_mapping_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Delete all course recommendations for a specific role mapping
    
    This endpoint removes:
    1. All recommendation records from table
    2. Associated vector embeddings and course data

    Args:
        role_mapping_id: UUID of the role mapping
        
    Returns:
        Deletion summary with counts and details
    """
    try:
        logger.info(f"Deleting course recommendations for role mapping: {role_mapping_id} by user: {current_user.user_id}")
        
        # Get all recommendation records for this role mapping
        recommendation_record = await crud_recommended_course.get_by_role_mapping_id(db, role_mapping_id, current_user.user_id)
        
        if not recommendation_record:
            logger.info(f"No course recommendations found for role mapping: {role_mapping_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No course recommendations found for role mapping: {role_mapping_id}"
            )
        
        if recommendation_record.status == RecommendationStatus.IN_PROGRESS:
            logger.info(f"Cannot delete recommendations while generation is currently in progress: {role_mapping_id}")
            raise HTTPException(
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                detail= {
                    'message':"Cannot delete recommendations while generation is currently in progress. Please wait for completion.",
                    'status': RecommendationStatus.IN_PROGRESS,
                }
            )
        
        await crud_recommended_course.delete_by_id(db, recommendation_record.id)
        
        success_message = f"Successfully deleted course recommendation records for role mapping '{role_mapping_id}'"
        
        result = {
            "message": success_message
        }
        
        logger.info(success_message)
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting course recommendations: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete course recommendations: {str(e)}"
        )

@router.delete("/course-recommendations/{role_mapping_id}/course/{course_id}")
async def delete_course(
    course_id: str = Path(..., description="Course identifier"),
    role_mapping_id: uuid.UUID = Path(..., description="Role mapping ID (required to identify the context)"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Delete a course. Automatically determines the course type by checking in order:
    1. Recommendations
    2. Suggestions
    3. User-added courses
    
    Args:
        course_id: UUID for user-added courses, or identifier for recommendations/suggestions
        role_mapping_id: Role mapping ID (required)
        
    Returns:
        Deletion confirmation with appropriate details
    """
    try:
        logger.info(f"Searching for course '{course_id}' in role mapping: {role_mapping_id} for deletion by user: {current_user.user_id}")
        
        # Step 1: Try recommendations first
        recommendation = await crud_recommended_course.get_by_role_mapping_id(db, role_mapping_id, current_user.user_id)
        
        if recommendation:
            # Check if course exists in recommendations
            course_found = any(
                course.get("identifier") == course_id 
                for course in recommendation.filtered_courses
            )
            
            if course_found:
                logger.info(f"Deleting recommended course '{course_id}' for role mapping: {role_mapping_id}")
                if recommendation.status == RecommendationStatus.IN_PROGRESS:
                    logger.info("Cannot modify course list while generation is currently in progress.")
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            'message': "Cannot modify course list while generation is currently in progress.",
                            'status': RecommendationStatus.IN_PROGRESS,
                        }
                    )
                
                # Delete from recommendations
                filtered_courses = [
                    course for course in recommendation.filtered_courses
                    if course.get("identifier") != course_id
                ]
                new_count = len(filtered_courses)
                
                await crud_recommended_course.update_status_and_data(
                    recommendation.id,
                    recommendation.vector_query,
                    recommendation.embedding,
                    recommendation.actual_courses,
                    filtered_courses
                )
                
                logger.info(f"Successfully deleted recommended course: {course_id}")
                return {
                    "message": f"Successfully deleted course '{course_id}' from recommendations",
                    "course_id": course_id,
                    "course_type": "recommendation",
                    "role_mapping_id": str(role_mapping_id),
                    "remaining_courses": new_count
                }
        
        # Step 2: Try suggestions
        suggested_course = await crud_suggested_course.get_by_role_mapping_and_user(db, role_mapping_id, current_user.user_id)
        
        if suggested_course and course_id in suggested_course.course_identifiers:
            logger.info(f"Deleting suggested course '{course_id}' for role mapping: {role_mapping_id}")
            # Delete from suggestions
            course_identifiers = [
                identifier for identifier in suggested_course.course_identifiers
                if identifier != course_id
            ]
            new_count = len(course_identifiers)
            update_records = {'course_identifiers': course_identifiers}
            await crud_suggested_course.update(db, suggested_course.id, update_records)
            
            logger.info(f"Successfully deleted suggested course: {course_id}")
            return {
                "message": f"Successfully deleted course '{course_id}' from suggestions",
                "course_id": course_id,
                "course_type": "suggestion",
                "role_mapping_id": str(role_mapping_id),
                "remaining_courses": new_count
            }
        
        # Step 3: Try as user-added course (check if valid UUID)
        logger.info(f"Attempting to delete as user-added course with ID: {course_id}")
        
        db_course = await crud_user_added_course.get_by_identifier(db, role_mapping_id, course_id, current_user.user_id)
        
        if db_course:
            course_name = db_course.name
            await crud_user_added_course.delete_by_identifier(db, role_mapping_id, course_id, current_user.user_id)
            
            logger.info(f"Successfully deleted user-added course: {course_name}")
            return {
                "message": f"User-added course '{course_name}' deleted successfully",
                "course_id": str(course_id),
                "course_type": "user_added",
                "role_mapping_id": str(role_mapping_id)
            }
        
        # If we reach here, course not found in any category
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Course '{course_id}' not found in recommendations, suggestions, or user-added courses for role mapping '{role_mapping_id}'"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting course: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete course: {str(e)}"
        )
