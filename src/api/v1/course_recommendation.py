import asyncio
import json
import os
import uuid
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from google import genai
from google.genai import types

from ...prompts.prompts import COURSE_SELECTION_SYSTEM_PROMPT, DESIGNNATION_GROUP_SYSTEM_PROMPT, VECTOR_QUERY_SYSTEM_PROMPT

from ...models.course_recommendation import RecommendationStatus
from ...models.user import User
from ...schemas.course_recommendation import (
    BulkRecommendationStatusResponse,
    RecommendCourseCreate,
    RecommendedCourseResponse,
)

from ...core.database import get_db_session
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
    location=settings.GOOOGLE_PROJECT_LOCATION_GLOBAL,
    vertexai=settings.GOOGLE_GENAI_USE_VERTEXAI,
    http_options=settings.GEMINI_HTTP_OPTIONS
)

embedding_client = genai.Client(
    api_key=settings.GOOGLE_API_KEY,
    vertexai=False,
    http_options=settings.GEMINI_HTTP_OPTIONS
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
        logger.exception(f"Error generating embedding for text '{text[:50]}...': {e}")
        return []

async def generate_contextual_queries(user_profile: str) -> Dict[str, Any]:
    """
    Generate three contextually rich search queries + a keyword list from the user profile:
    - keyword_query     : compact phrase for keyword_embedding vector search
    - description_query : narrative paragraph for description_embedding vector search
    - combined_query    : multi-angle rich query for combined_embedding vector search
    - search_keywords   : list of 10-15 domain/skill terms for Postgres keyword search

    All outputs are sector/domain-aware and non-generic.
    """
    logger.info("Generating contextual queries from user profile")

    user_part = types.Part.from_text(text=f"Role Profile:\n{user_profile}")
    contents = [types.Content(role="user", parts=[user_part])]

    config = types.GenerateContentConfig(
        temperature=0.4,
        top_p=0.95,
        # max_output_tokens=2048,
        safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
        ],
        response_mime_type="application/json",
        response_schema={
            "type": "OBJECT",
            "properties": {
                "keyword_query":     {"type": "STRING"},
                "description_query": {"type": "STRING"},
                "combined_query":    {"type": "STRING"},
                "search_keywords":   {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["keyword_query", "description_query", "combined_query", "search_keywords"],
        },
        system_instruction=[types.Part.from_text(text=VECTOR_QUERY_SYSTEM_PROMPT)],
    )

    response = await client.aio.models.generate_content(
        model=settings.GEMINI_PRO_MODEL_NAME,
        contents=contents,
        config=config,
    )
    logger.info("Contextual queries generated successfully")
    if not response.text:
        logger.error(f"LLM returned empty response for contextual queries: {response}")
        raise Exception("generate_contextual_queries: LLM returned empty response")
    return json.loads(response.text)

async def infer_designation_group(user_profile: str) -> str:
    """
    Ask the LLM to reason about the full role profile and classify the designation
    into Group A/B (senior/gazetted officers) or Group C/D (supporting/clerical staff).
    Returns 'AB' or 'CD'.
    """
    user_part = types.Part.from_text(text=f"Role Profile:\n{user_profile}")

    config = types.GenerateContentConfig(
        temperature=0,
        max_output_tokens=256,
        safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
        ],
        response_mime_type="application/json",
        response_schema={
            "type": "OBJECT",
            "properties": {"group": {"type": "STRING", "enum": ["AB", "CD"]}},
            "required": ["group"],
        },
        system_instruction=[types.Part.from_text(text=DESIGNNATION_GROUP_SYSTEM_PROMPT)],
    )

    try:
        response = await client.aio.models.generate_content(
            model=settings.GEMINI_FLASH_MODEL_NAME,
            contents=[types.Content(role="user", parts=[user_part])],
            config=config,
        )
        if not response.text:
            logger.warning("Designation group LLM returned empty response, defaulting to AB")
            return "AB"
        result = json.loads(response.text)
        group = result.get("group", "AB")
        logger.info(f"LLM classified designation group as: {group}")
        return group
    except Exception as e:
        logger.warning(f"Designation group inference failed, defaulting to AB: {e}")
        return "AB"


async def get_filtered_courses_by_llm(
    courses_prompt: str,
    user_profile: str,
    organisation: str,
    designation_group: str,
) -> str:
    """
    LLM-based course selection and scoring with:
    - Provider priority (own-org courses preferred)
    - Domain-mix enforcement by designation group
    - Sector-specific domain inclusion
    - Topic/type diversity within domain courses
    """
    logger.info("Filtering candidate courses through LLM")

    if designation_group == "AB":
        mix_rule = "Domain: ≥50%, Behavioral: ~25%, Functional: ~25%"
    else:
        mix_rule = "Domain: ~40%, Behavioral: ~30%, Functional: ~30%"
    # ({mix_rule})
    

    user_part = types.Part.from_text(text=f"""
Role Profile:
{user_profile}

Own Organisation: {organisation or 'N/A'}

Candidate Courses:
{courses_prompt}
""")

    response_schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "identifier":        {"type": "STRING"},
                "course":            {"type": "STRING"},
                "relevancy":         {"type": "INTEGER"},
                "rationale":         {"type": "STRING"},
            },
            "required": ["identifier", "course", "relevancy", "rationale"],
        },
    }

    config = types.GenerateContentConfig(
        temperature=0,
        top_p=1,
        # max_output_tokens=8192,
        safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
        ],
        response_mime_type="application/json",
        response_schema=response_schema,
        system_instruction=[types.Part.from_text(text=COURSE_SELECTION_SYSTEM_PROMPT)],
        thinking_config=types.ThinkingConfig(include_thoughts=False, thinking_budget=2048),
    )

    response = await client.aio.models.generate_content(
        model=settings.GEMINI_PRO_MODEL_NAME,
        contents=[types.Content(role="user", parts=[user_part])],
        config=config,
    )
    if not response.text:
        logger.error(f"LLM filtering empty response — failed to inspect:  {response}")
        return "[]"
    return response.text

async def get_general_courses_from_gemini(user_profile) -> List[Dict[str, Any]]:
    """
    Fetches general courses from Gemini based on the designation and department.
    """
    # Disabled for temporary reasons. Remove below line to enable Gemini fetching of general courses. 
    return []
    logger.info("Fetching the general courses across the learning platforms")
    generate_content_config = types.GenerateContentConfig(
        system_instruction=f"""
        You are an expert in civil service training and development.
        Your role is to recommend highly relevant and foundational courses that would help professionals excel in their designation within government/administrative organizations.

        # Research & Recommendation Guidelines:
        1. Search across credible and accessible learning platforms, including but not limited to:
            Coursera, edX, Udemy, FutureLearn, SWAYAM, NPTEL, Khan Academy, WHO, Harvard Online, MIT OCW, Stanford Online, LinkedIn Learning, etc.
            - Prefer globally credible and India-contextualized content.
            - Do not include iGOT/Karmayogi links.

        2. Course Selection Criteria:
            - Recommend 10–15 courses that are universally essential for this designation.
            - Courses must strengthen Behavioral, Functional, and Domain competencies.
            - Ensure recommendations are active, course-specific, and not generic category pages.
            - Do not include fictional or AI-generated course names. Recommend only courses that exist publicly and are accessible.

        3. Quality Control:
            - Avoid duplicates.
            - Ensure public links are correct and accessible.
            - Keep rationales concise and role-relevant.
            - Course name should be the same as given in the webpage.
        
        For each course, provide the following information in a structured JSON format:
        - course: The full name of the course.
        - platform: The name of the platform where the course is hosted (e.g., Coursera, edX, Udemy).
        - relevancy: An integer from 0 to 100, indicating high relevancy.
        - rationale: A brief, 1-2 sentence explanation of why this course is essential.
        - language: The language of the specific course (e.g., en, hi).
        - public_link: An actual public URL to the specific course.
        - competencies: An array of competency objects. 
          Each object should have competencyAreaName, competencyThemeName, and competencySubThemeName.
        Ensure the output is a JSON array of objects.

        **OUTPUT FORMAT REQUIRED:**
        Provide the output as a **direct JSON array of objects**. 
        **IMPORTANT:** Do **NOT** enclose the JSON within markdown code blocks (e.g., do not use ```json ... ``` or ``` ... ```). The output must be *only* the JSON array itself.
        """,
        temperature=0.5,
        # Remove tools unless you really want google_search
        tools=[{"google_search": {}}],

        safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF")
        ],
        # response_mime_type="application/json",
        # response_schema=schema,
    )

    try:
        msg1_text1 = types.Part.from_text(
            text=f"Here's the user role context: {user_profile}"
        )
        contents = [types.Content(role="user", parts=[msg1_text1])]

        response = await client.aio.models.generate_content(
            model=settings.GEMINI_PRO_MODEL_NAME,
            contents=contents,
            config=generate_content_config,
        )
        
        text_response = response.text
        if not text_response:
            print("Gemini response was empty or not in text format.")
            return []

        
        text_response = text_response.replace("```json", '')
        text_response = text_response.replace("```", '')
        # # Parse JSON
        general_courses = json.loads(text_response)

        # Add identifiers
        for course in general_courses:
            course['identifier'] = str(uuid.uuid4())
            course['is_public'] = True
        logger.info("Fetched general courses from Gemini")
        return general_courses

    except Exception as e:
        print("Gemini raw response (before failure):", locals().get("response", "No response"))
        print(f"Error fetching general courses from Gemini: {e}")
        return []

def _build_competency_query(competencies: list) -> str:
    """
    Mechanically construct a query string from the user's competency JSONB list
    using the same taxonomy format that course embeddings were built with:
      "Type: {area} -> Theme: {theme} -> Sub-Theme: {sub_theme}"

    This is zero-cost (no LLM call) and produces a query that lives in the same
    vector space as combined_embedding, maximising retrieval of functional and
    behavioral courses whose embeddings contain this exact taxonomy phrasing.
    """
    if not competencies:
        return ""
    parts = []
    for c in competencies:
        area  = c.get("competencyAreaName") or c.get("type") or ""
        theme = c.get("competencyThemeName") or c.get("theme") or ""
        sub   = c.get("competencySubThemeName") or c.get("sub_theme") or ""
        if area or theme:
            parts.append(f"Type: {area} -> Theme: {theme} -> Sub-Theme: {sub}")
    if not parts:
        return ""
    return "Training course covering the following government competencies: " + " | ".join(parts)


def _build_competency_query_by_type(competencies: list, competency_type: str) -> str:
    """
    Same as _build_competency_query, but restricted to competency entries whose
    competencyAreaName matches competency_type ("functional" or "behavioural").

    Keeping functional and behavioural queries separate (instead of one query built
    from all competencies) avoids blending the two vector spaces together, so each
    fetch_competency_typed_courses call is scored against its own matching competencies.
    """
    if not competencies:
        return ""
    filtered = [
        c for c in competencies
        if competency_type in (c.get("competencyAreaName") or c.get("type") or "").lower()
    ]
    return _build_competency_query(filtered)


async def process_recommendation_task(
    recommendation_id: uuid.UUID,
    user_profile: str,
    ministry_state_name: str,
    department_name: str,
    raw_competencies: list = None,
):
    """
    Background task: hybrid multi-embedding vector search + LLM filtering.

    Steps:
      1. Verify record exists
      2. Generate 3 contextual queries (keyword, description, combined)
      3. Embed all 3 queries in parallel
      4. Hybrid weighted search: keywords 40%, description 20%, combined 40%
      5. Deduplicate and enrich candidates with metadata
      6. Build enriched prompt with org/competency context
      7. LLM selects final courses with domain-mix + provider priority rules
      8. Enrich selected courses and persist
    """
    logger.info(f"Starting course recommendation background task for {recommendation_id}")

    try:
        # 1. Verify record exists
        rec_record = await crud_recommended_course.get_by_id(recommendation_id)
        
        if not rec_record:
            raise Exception(f"Recommendation record not found for ID: {recommendation_id}. Aborting task.")

        # 2. Generate 3 contextual queries + domain search keywords (single LLM call)
        queries = await generate_contextual_queries(user_profile)
        keyword_query     = queries.get("keyword_query", "")
        description_query = queries.get("description_query", "")
        combined_query    = queries.get("combined_query", "")
        search_keywords   = queries.get("search_keywords", [])
        
        all_queries = [{
            "keyword_query": keyword_query,
            "description_query": description_query,
            "combined_query": combined_query,
            "search_keywords": search_keywords
        }]

        # Mechanically built competency taxonomy queries (zero-cost, no LLM call) — kept
        # separate per type so the functional and behavioural searches are never blended
        # into a single combined vector.
        functional_competency_query  = _build_competency_query_by_type(raw_competencies or [], "functional")
        behavioural_competency_query = _build_competency_query_by_type(raw_competencies or [], "behavioral")
        
        # 3. Embed all queries in parallel
        kw_emb_list, desc_emb_list, comb_emb_list, func_comp_emb_list, behav_comp_emb_list = await asyncio.gather(
            get_embedding(keyword_query),
            get_embedding(description_query),
            get_embedding(combined_query),
            get_embedding(functional_competency_query),
            get_embedding(behavioural_competency_query),
        )
        if not kw_emb_list or not desc_emb_list or not comb_emb_list:
            raise Exception("Failed to generate one or more embeddings")

        kw_emb   = kw_emb_list[0].values
        desc_emb = desc_emb_list[0].values
        comb_emb = comb_emb_list[0].values
        func_comp_emb  = func_comp_emb_list[0].values if func_comp_emb_list else None
        behav_comp_emb = behav_comp_emb_list[0].values if behav_comp_emb_list else None

        # 4. Vector search + Postgres keyword search + competency-typed searches in parallel
        vector_results, kw_results, func_results, behav_results = await asyncio.gather(
            crud_recommended_course.fetch_hybrid_search_courses(
                keyword_emb=kw_emb,
                description_emb=desc_emb,
                combined_emb=comb_emb,
                limit=100,
            ),
            crud_recommended_course.fetch_keyword_search_courses(
                keywords=search_keywords,
                limit=40,
            ),
            # Pre-filtered functional courses (competencyAreaName LIKE '%functional%'),
            # scored against the functional-only competency embedding.
            crud_recommended_course.fetch_competency_typed_courses(
                combined_emb=func_comp_emb or comb_emb,
                competency_type="functional",
                limit=40,
            ),
            # Pre-filtered behavioral courses (competencyAreaName LIKE '%behavioural%'),
            # scored against the behavioural-only competency embedding.
            crud_recommended_course.fetch_competency_typed_courses(
                combined_emb=behav_comp_emb or comb_emb,
                competency_type="behavioural",
                limit=40,
            ),
        )

        # 5. Merge & deduplicate: vector score normalised to [0,1]; keyword hits get a
        #    bonus score of 0.15 (max keyword_score=3 → normalise to 0-0.15). Functional and
        #    behavioural hits get a flat bonus of 0.10 to ensure they surface in the final pool.
        seen: Dict[str, Dict[str, Any]] = {}
        for identifier, name, score in vector_results:
            seen[identifier] = {"identifier": identifier, "name": name, "distance": float(score)}

        for identifier, name, kw_score in kw_results:
            bonus = min(float(kw_score) / 3.0, 1.0) * 0.15
            if identifier in seen:
                seen[identifier]["distance"] = seen[identifier]["distance"] + bonus
            else:
                seen[identifier] = {"identifier": identifier, "name": name, "distance": bonus}

        for identifier, name, score in (func_results or []) + (behav_results or []):
            if identifier in seen:
                seen[identifier]["distance"] = max(seen[identifier]["distance"], float(score)) + 0.10
            else:
                seen[identifier] = {"identifier": identifier, "name": name, "distance": float(score) + 0.10}

        all_candidates = sorted(seen.values(), key=lambda c: c["distance"], reverse=True)
        logger.info(
            f"Merged candidates: {len(all_candidates)} total "
            f"({len(vector_results)} vector + {len(kw_results)} keyword + "
            f"{len(func_results or [])} functional + {len(behav_results or [])} behavioural hits)"
        )

        # 6. Fetch enriched metadata for candidates
        all_identifiers = [c["identifier"] for c in all_candidates]
        if all_identifiers:
            identifiers_str = ", ".join(f"'{id}'" for id in all_identifiers)
            metadata_rows = await crud_recommended_course.fetch_course_metadata(identifiers_str)
            metadata_map = {row.identifier: row for row in metadata_rows}
        else:
            metadata_map = {}

        # 7. Build LLM prompt with org/competency context
        candidate_lines = []
        for c in all_candidates:
            meta = metadata_map.get(c["identifier"])
            _org_raw = getattr(meta, "organisation", None)
            if isinstance(_org_raw, list):
                org_info = ", ".join(str(o) for o in _org_raw if o)
            else:
                org_info = str(_org_raw) if _org_raw else ""
            c["competencies"] = getattr(meta, "competencies_v6", None)
            is_own_org = "YES" if (ministry_state_name and org_info and ministry_state_name.lower() in org_info.lower()) else "NO"
            if is_own_org == "NO" and department_name and org_info and department_name.lower() in org_info.lower():
                is_own_org = "YES"

            candidate_lines.append(
                f"Course ID: {c['identifier']} | "
                f"Course Name: {c['name']} | "
                f"Course Description: {getattr(meta, 'description', None)} | "
                f"Course Keywords: {getattr(meta, 'keywords', None)} | "
                f"Similarity: {c['distance']:.4f} | "
                f"Organisation: {org_info or 'N/A'} | "
                f"Own Org: {is_own_org} | "
                # f"{comp_names}"
            )

        courses_prompt = "\n".join(candidate_lines)

        # 8. Determine designation group for mix ratios (LLM-reasoned)
        # designation_group = await infer_designation_group(user_profile)
        designation_group = None

        # 9. LLM filtering + general courses (parallel)
        filtered_courses_json, general_courses = await asyncio.gather(
            get_filtered_courses_by_llm(courses_prompt, user_profile, department_name or ministry_state_name, designation_group),
            get_general_courses_from_gemini(user_profile),
        )

        filtered_courses = json.loads(filtered_courses_json)

        # 10. Enrich filtered courses with full metadata
        filtered_identifiers = [c["identifier"] for c in filtered_courses]
        if filtered_identifiers:
            f_identifiers_str = ", ".join(f"'{id}'" for id in filtered_identifiers)
            enriched_rows = await crud_recommended_course.fetch_course_metadata(f_identifiers_str)
            enriched_map = {row.identifier: row for row in enriched_rows}
        else:
            enriched_map = {}

        logger.info(
            f"LLM filtered {len(filtered_courses)} courses from {len(all_candidates)} candidates "
            f"and {len(general_courses)} general courses"
        )
        filtered_courses = [course for course in filtered_courses if course["identifier"] in enriched_map]
        logger.info(f"After enrichment, {len(filtered_courses)} courses remain with valid metadata")
        for course in filtered_courses:
            course["is_public"] = False
            meta = enriched_map.get(course["identifier"])
            if meta:
                course["course"] = meta.name
                course["competencies"] = meta.competencies_v6
                course["duration"] = meta.duration
                _org = meta.organisation
                course["organisation"] = (
                    ", ".join(str(o) for o in _org if o) if isinstance(_org, list) else (_org or None)
                )

        final_filtered_courses = filtered_courses + general_courses
        final_filtered_courses = [
            course for course in final_filtered_courses
            if course.get("relevancy", 0) >= settings.COURSE_RECOMMENDATION_MIN_RELEVANCY
        ]
        final_filtered_courses.sort(key=lambda course: course.get("relevancy", 0), reverse=True)

        # 11. Persist
        await crud_recommended_course.update_status_and_data(
            recommendation_id,
            json.dumps(all_queries, ensure_ascii=False),
            kw_emb,
            all_candidates,
            final_filtered_courses,
        )

        logger.info(
            f"Course Recommendation task completed for {recommendation_id}: "
            f"{len(final_filtered_courses)} courses with relevancy >= 80 "
            f"(from {len(filtered_courses)} iGOT + {len(general_courses)} public candidates)"
        )

    except Exception as e:
        logger.exception(f"Course Recommendation background task failed for {recommendation_id}:")
        try:
            await crud_recommended_course.update_status_to_failed(recommendation_id, str(e))
        except Exception:
            logger.exception(f"Failed to update course recommendation record to FAILED for {recommendation_id}")

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
        logger.info(f"Generate course recommendations request received for role mapping: {role_mapping_id} by user: {current_user.user_id}")
        
        # Get role mapping
        role_mapping = await crud_role_mapping.get_by_id_and_user(db, role_mapping_id, current_user.user_id)
        if not role_mapping:
            logger.warning(f"Role mapping not found for ID: {role_mapping_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found."
            )
        
        existing_recommendation = await crud_recommended_course.get_by_role_mapping_id(db, role_mapping_id, current_user.user_id)
        if existing_recommendation:
            logger.info(f"Found existing recommendation for Role mapping ID: {role_mapping_id}")
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
                return existing_recommendation
        
        competencies_json = json.dumps(role_mapping.competencies, indent=2) if role_mapping.competencies else "[]"
        user_profile = f"""
Ministry/State/Organisation: {role_mapping.state_center_name}
Department Name: {role_mapping.department_name if role_mapping.department_name else 'N/A'}
Sector: {role_mapping.sector_name if role_mapping.sector_name else 'N/A'}
Designation Name: {role_mapping.designation_name}
Wing/Division/Section: {role_mapping.wing_division_section if role_mapping.wing_division_section else 'N/A'}
Roles & Responsibilities: {role_mapping.role_responsibilities}
Key Activities: {role_mapping.activities}
Competencies (with definitions):
{competencies_json}
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
            user_profile,
            role_mapping.state_center_name or "",
            role_mapping.department_name or "",
            role_mapping.competencies or [],
        )

        logger.info(f"Course recommendation generation initiated for role mapping: {role_mapping_id}")
        return new_recommendation
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error in generate course recommendations endpoint:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate course recommendations. Please try again later."
        )

@router.get("/course-recommendations/bulk-status", response_model=BulkRecommendationStatusResponse)
async def get_bulk_course_recommendation_status(
    state_center_id: str = Query(..., description="ID of the associated state/center"),
    department_id: Optional[str] = Query(None, description="ID of the associated department"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Get status of all in-progress course recommendations for a state/center (and optional department)"""
    try:
        logger.info(
            f"Fetching bulk course recommendation status for state_center: {state_center_id}, "
            f"department: {department_id}, user: {current_user.user_id}"
        )

        in_progress_recommendations = await crud_recommended_course.get_in_progress_by_scope(
            db,
            current_user.user_id,
            state_center_id,
            department_id,
        )

        items = [
            {
                "role_mapping_id": recommendation.role_mapping_id,
                "recommendation_id": recommendation.id,
                "status": recommendation.status,
            }
            for recommendation in in_progress_recommendations
        ]

        return BulkRecommendationStatusResponse(items=items)
    except Exception as e:
        logger.exception("Error in bulk course recommendation status endpoint:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch bulk course recommendation status. Please try again later."
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
        logger.info(f"Successfully fetched course recommendations")
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
