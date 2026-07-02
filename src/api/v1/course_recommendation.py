import asyncio
import json
import os
import uuid
from typing import Any, Dict, List
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from google import genai
from google.genai import types

from ...models.course_recommendation import RecommendationStatus
from ...models.user import User
from ...schemas.course_recommendation import RecommendCourseCreate, RecommendedCourseResponse

from ...core.database import get_db_session
from ...core.logger import logger
from ...core.configs import settings

from ...crud.course_recommendation import crud_recommended_course, CONTENT_RERANK_ENABLED
from ...crud.role_mapping import crud_role_mapping
from ...crud.course_suggestion import crud_suggested_course
from ...crud.user_added_course import crud_user_added_course

from ...api.dependencies import get_current_active_user

router = APIRouter(tags=["Course Recommendations"])

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS
client = genai.Client(
    project=settings.GOOGLE_PROJECT_ID,
    location=settings.GOOGLE_PROJECT_LOCATION,
    vertexai=True
)

embedding_client = genai.Client(
    api_key=settings.GOOGLE_API_KEY
)

# Vertex client on the GLOBAL endpoint, for models served only there (e.g. gemini-3.5-flash)
client_global = genai.Client(
    vertexai=True,
    project=settings.GOOGLE_PROJECT_ID,
    location="global",
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


async def get_content_embedding(text: str) -> list:
    """Embed a query in the LEGACY content-embedding space for content_rerank ONLY.

    The main search uses gemini-embedding-2 @ 1536 (get_embedding) against
    course_metadata_v3. But `content_embeddings` was built with the legacy
    text-multilingual-embedding-002 model (768-dim), a DIFFERENT vector space.
    To rerank against it we must embed the query with that same legacy model --
    reusing the 1536-dim main vector would be dimension-incompatible and, even at
    a matching size, semantically meaningless across models.

    Uses the regional Vertex `client` (the legacy model is a Vertex text-embedding
    model), mirroring how content_embeddings were generated. Returns [] on failure
    so the caller can simply skip content_rerank without breaking the request.
    """
    if not text.strip():
        return []
    try:
        response = await client.aio.models.embed_content(
            model=settings.CONTENT_EMBEDDING_MODEL,
            contents=text,
            config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY"),
        )
        return response.embeddings
    except Exception as e:
        logger.warning(f"get_content_embedding failed; content_rerank will be skipped: {e}")
        return []


async def generate_vector_query(query):
    logger.info(f"Generating vector query for this profile :: {query[:50]}")
    user_part = types.Part.from_text(text=f"""
    You are given a government official's role profile:
    {query}

    Write a concise search query (3-5 sentences, ~60-100 words) describing the IDEAL training courses for THIS specific role.
    It will be used for combined semantic + keyword retrieval over a training-course catalog.

    Requirements:
    - Calibrate to the SENIORITY / LEVEL implied by the profile. For senior or strategic roles (e.g. Director-General,
      Secretary, Commissioner, Joint Secretary, senior executives) emphasise ADVANCED, strategic, leadership and
      policy-level courses, and explicitly EXCLUDE introductory / foundational / basic / awareness-level courses.
      For junior or operational roles, emphasise foundational and skill-building courses.
    - Capture the role's core domain, key responsibilities and required competencies in natural descriptive language,
      and also include the important domain terms / keywords (so keyword matching works too).
    - Be specific to this role; avoid generic filler.
    """)
    system_instruction = (
        "You are an expert at writing retrieval queries that match training courses to a government role's LEVEL and domain. "
        "Always calibrate the seniority of the recommended courses to the role (advanced for senior roles, foundational for junior). "
        "Output ONLY the raw query text - no preamble (e.g. 'Here is the query'), no headings, no rationale, no bullet points, no markdown."
    )
    
    model = "gemini-2.5-pro"
    contents = [
        types.Content(
            role="user",
            parts=[
                user_part
            ]
        ),
    ]

    generate_content_config = types.GenerateContentConfig(
        temperature=0.3,
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
        system_instruction=[types.Part.from_text(text=system_instruction)],
        thinking_config=types.ThinkingConfig(
            thinking_budget=-1,
        ),
    )

    response = await client.aio.models.generate_content(
        model=model,
        contents=contents,
        config=generate_content_config,
    )
    
    response_text = response.text or ""
    # Post-process: strip markdown code blocks if the model wrapped the response
    if response_text.startswith("```"):
        lines = response_text.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            if lines[-1].startswith("```"):
                response_text = "\n".join(lines[1:-1])
            else:
                response_text = "\n".join(lines[1:])
    
    # Remove any headers or rationales that might have leaked
    if "### Rationale" in response_text:
        response_text = response_text.split("### Rationale")[0]
    elif "Rationale:" in response_text:
        response_text = response_text.split("Rationale:")[0]
        
    cleaned_query = response_text.strip()
    logger.info(f"Vector query generated successfully: '{cleaned_query[:60]}...'")
    return cleaned_query

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
    
    model = "gemini-2.5-flash"
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

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=generate_content_config,
            )
            res_text = response.text
            if res_text.startswith("```"):
                lines = res_text.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].startswith("```"):
                    lines = lines[:-1]
                res_text = "\n".join(lines).strip()
            
            parsed = json.loads(res_text)
            logger.info("Filtered courses successfully")
            return parsed
        except json.JSONDecodeError as je:
            logger.warning(f"JSON decode failed on attempt {attempt + 1} for course filtering: {je}. Raw response: {response.text[:200] if 'response' in locals() else 'None'}")
            if attempt == max_retries - 1:
                raise je
        except Exception as e:
            logger.warning(f"Error on attempt {attempt + 1} for course filtering: {e}")
            if attempt == max_retries - 1:
                raise e

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
            model="gemini-2.5-pro",
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

# ---- Recommendation composition: provider priority + competency-type mix ----
_COMPETENCY_TYPES = ("Domain", "Behavioural", "Functional")
_TYPE_TIEBREAK = {"Domain": 3, "Functional": 2, "Behavioural": 1}  # domain wins ties (it's emphasised)


async def classify_designation_group(designation_name: str) -> str:
    """Classify a designation into "AB" (Group A/B, gazetted/officer) or "CD"
    (Group C/D, clerical/support). Uses gemini-3.5-flash on the global endpoint
    with thinking disabled (so it returns a token instead of burning the budget).
    Falls back to "AB" (officer) on any error."""
    name = (designation_name or "").strip()
    if not name:
        return "AB"
    try:
        resp = await client_global.aio.models.generate_content(
            model="gemini-3.5-flash",
            contents=(
                'Classify this Indian government designation into its service group. '
                'Reply with EXACTLY one token: "AB" for Group A or B (gazetted / officer / '
                'supervisory / policy), or "CD" for Group C or D (clerical / support / '
                f'operational).\nDesignation: "{name}"'
            ),
            config=types.GenerateContentConfig(
                temperature=0, max_output_tokens=16,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        out = (resp.text or "").strip().upper()
        return "CD" if "CD" in out else "AB"
    except Exception as e:
        logger.warning(f"classify_designation_group('{name}') failed, defaulting to AB: {e}")
        return "AB"


def _primary_competency_type(course: Dict[str, Any]):
    """The course's dominant competency area (Domain/Behavioural/Functional), or None."""
    comps = course.get("competencies") or []
    counts: Dict[str, int] = {}
    for c in comps:
        area = (c or {}).get("competencyAreaName")
        if area in _COMPETENCY_TYPES:
            counts[area] = counts.get(area, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda a: (counts[a], _TYPE_TIEBREAK.get(a, 0)))


def _org_matches(course: Dict[str, Any], org_terms: List[str]) -> bool:
    orgs = course.get("organisation") or []
    if isinstance(orgs, str):
        orgs = [orgs]
    hay = " ".join(o.lower() for o in orgs if o)
    return bool(hay) and any(t and (t in hay or hay in t) for t in org_terms)


def compose_recommendations(courses: List[Dict[str, Any]], group: str, org_terms: List[str]) -> List[Dict[str, Any]]:
    """Reorder the enriched, relevancy-scored courses to (a) approach the group's
    Domain/Behavioural/Functional mix and (b) prioritise the learner's own
    organisation, filling the rest by relevance. Non-destructive (no course dropped,
    only reordered); degrades to relevance order when competency/org data is thin.
    """
    if not courses:
        return courses
    q = ({"Domain": settings.MIX_AB_DOMAIN, "Behavioural": settings.MIX_AB_BEHAVIOURAL, "Functional": settings.MIX_AB_FUNCTIONAL}
         if group == "AB" else
         {"Domain": settings.MIX_CD_DOMAIN, "Behavioural": settings.MIX_CD_BEHAVIOURAL, "Functional": settings.MIX_CD_FUNCTIONAL})

    for c in courses:
        c["_type"] = _primary_competency_type(c)
        c["_org"] = _org_matches(c, org_terms) if org_terms else False

    def _key(c):  # provider-first (if enabled), then relevance
        provider = 1 if (settings.PROVIDER_PRIORITY_ENABLED and c.get("_org")) else 0
        return (provider, c.get("relevancy") or 0)

    buckets: Dict[str, List[Dict[str, Any]]] = {t: [] for t in _COMPETENCY_TYPES}
    other: List[Dict[str, Any]] = []
    for c in courses:
        (buckets[c["_type"]] if c["_type"] in buckets else other).append(c)
    for lst in list(buckets.values()) + [other]:
        lst.sort(key=_key, reverse=True)

    n = len(courses)
    if settings.COMPETENCY_MIX_ENABLED:
        targets = {t: int(round(q.get(t, 0) * n)) for t in _COMPETENCY_TYPES}
        result: List[Dict[str, Any]] = []
        for t in _COMPETENCY_TYPES:
            result.extend(buckets[t][:targets[t]])
        chosen = {id(c) for c in result}
        leftovers = [c for c in courses if id(c) not in chosen]
        leftovers.sort(key=_key, reverse=True)
        result.extend(leftovers)
    else:
        result = sorted(courses, key=_key, reverse=True)

    # Provider priority as the top-level ordering (stable: preserves the mix order within each side)
    if settings.PROVIDER_PRIORITY_ENABLED and org_terms:
        result.sort(key=lambda c: 1 if c.get("_org") else 0, reverse=True)

    for c in result:
        c.pop("_type", None)
        c.pop("_org", None)
    return result


async def process_recommendation_task(recommendation_id: uuid.UUID, user_profile: str):
    """
    Background task to perform LLM calls and Vector Search.
    Manages its own DB session.
    """
    logger.info(f"Background task started for recommendation_id: {recommendation_id}")
    
    try:
        # 1. Retrieve the record to update
        rec_record = await crud_recommended_course.get_by_id(recommendation_id)
        if not rec_record:
            logger.error(f"Record {recommendation_id} not found in background task")
            return

        # Get role mapping details to retrieve competencies for boosting
        role_mapping = None
        themes = []
        sub_themes = []
        if rec_record.role_mapping_id:
            role_mapping = await crud_role_mapping.get_by_id(rec_record.role_mapping_id)
            if role_mapping and role_mapping.competencies:
                for comp in role_mapping.competencies:
                    if comp.get("theme"):
                        themes.append(comp.get("theme"))
                    if comp.get("sub_theme"):
                        sub_themes.append(comp.get("sub_theme"))

        # 2. Generate Vector Query
        query_text = await generate_vector_query(user_profile)
        
        # 3. Generate Embedding
        embedding_list = await get_embedding(query_text)
        if not embedding_list:
            raise Exception("Failed to generate embeddings")
        embedding_values = embedding_list[0].values
        # 4. Hybrid DB Search (dense + lexical RRF)
        result = await crud_recommended_course.fetch_vector_search_courses(
            embedding_values,
            query_text=query_text,
            themes=themes,
            sub_themes=sub_themes
        )
        courses = []
        for name, identifier, distance in result:
            courses.append({
                "name": name,
                "identifier": identifier,
                "distance": distance
            })

        # 4b. (optional) Content-embedding rerank: refine the hybrid candidates
        # against actual course content, then narrow before the LLM stage.
        # content_embeddings is in the LEGACY 768-dim space (text-multilingual-
        # embedding-002), NOT the 1536-dim gemini-embedding-2 space of `embedding_values`.
        # So embed the query separately in that legacy space and pass THAT vector.
        if CONTENT_RERANK_ENABLED:
            content_embedding_list = await get_content_embedding(query_text)
            if content_embedding_list:
                courses = await crud_recommended_course.content_rerank(
                    courses, content_embedding_list[0].values
                )
            else:
                logger.warning("content_rerank skipped: no legacy content embedding produced")

        # 5. Prepare LLM inputs
        relevant_courses_prompt = [f"Course Name: {c['name']}, Course ID: {c['identifier']}" for c in courses]
        relevant_courses_prompt = "\n".join(relevant_courses_prompt)
        # 6. Run Concurrent LLM Tasks
        tasks = [get_filtered_courses_by_llm(relevant_courses_prompt, user_profile), get_general_courses_from_gemini(user_profile)]
        filtered_courses, general_courses = await asyncio.gather(*tasks)
        
        # 7. Process Results (already parsed)
        
        # 8. Enrich Data (Fetch competencies)
        filtered_identifiers = [course['identifier'] for course in filtered_courses]
        if filtered_identifiers:
            identifiers_str = ", ".join(f"'{id}'" for id in filtered_identifiers)
            competencies_result = await crud_recommended_course.fetch_course_metadata(identifiers_str)
            competencies_map = {row.identifier: row for row in competencies_result}
        else:
            competencies_map = {}

        for course in filtered_courses:
            course["is_public"] = False
            if course['identifier'] in competencies_map:
                data = competencies_map.get(course['identifier'])
                course['competencies'] = data.competencies_v6
                course['duration'] = data.duration
                course['organisation'] = data.organisation
            else:
                course['competencies'] = None
                course['duration'] = None
                course['organisation'] = None

        # 8b. Compose the final list: provider priority + competency-type mix by group.
        if role_mapping and (settings.COMPETENCY_MIX_ENABLED or settings.PROVIDER_PRIORITY_ENABLED):
            group = await classify_designation_group(role_mapping.designation_name) if settings.COMPETENCY_MIX_ENABLED else "AB"
            org_terms = [t for t in [
                (role_mapping.state_center_name or "").strip().lower(),
                (role_mapping.department_name or "").strip().lower(),
            ] if t]
            filtered_courses = compose_recommendations(filtered_courses, group, org_terms)
            logger.info(f"composed recommendations: group={group}, provider_terms={org_terms}, n={len(filtered_courses)}")

        final_filtered_courses = filtered_courses + general_courses

        # 9. Update DB Record to COMPLETED
        await crud_recommended_course.update_status_and_data(
            recommendation_id,
            query_text,
            embedding_values,
            courses,
            final_filtered_courses,
        )
        
        logger.info(f"Course Recommendation Background task completed successfully for {recommendation_id}")

    except Exception as e:
        logger.error(f"Course Recommmendation Background task failed for {recommendation_id}: {str(e)}")
        # Update record to FAILED
        try:
            await crud_recommended_course.update_status_to_failed(recommendation_id, str(e))
        except Exception as db_e:
            logger.error(f"CRITICAL: Failed to update status to FAILED: {db_e}")

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
        role_mapping
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
                db.delete(existing_recommendation)
                db.commit()
        
        user_profile = f"""
        Ministry/State Name: {role_mapping.state_center_name}
        Department Name: {role_mapping.department_name if role_mapping.department_name else 'N/A'}
        Designation Name: {role_mapping.designation_name}
        Roles & Responsibilities: {role_mapping.role_responsibilities}
        Activities: {role_mapping.activities}
        Competencies: {json.dumps(role_mapping.competencies, indent=2)}
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
        logger.error(f"Error initiating course recommendation: {str(e)}")
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
