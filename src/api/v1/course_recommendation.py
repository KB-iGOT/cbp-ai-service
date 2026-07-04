import asyncio
import json
import os
import re
import uuid
from typing import Any, Dict, List
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from google import genai
from google.genai import types

from ...prompts.prompts import COURSE_SELECTION_SYSTEM_PROMPT, DESIGNNATION_GROUP_SYSTEM_PROMPT, VECTOR_QUERY_SYSTEM_PROMPT, SENIORITY_GROUP_SYSTEM_PROMPT

from ...models.course_recommendation import RecommendationStatus
from ...models.user import User
from ...schemas.course_recommendation import RecommendCourseCreate, RecommendedCourseResponse

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
        model="gemini-3.1-pro-preview",
        contents=contents,
        config=config,
    )
    logger.info("Contextual queries generated successfully")
    if not response.text:
        print(response.text)
        raise Exception("generate_contextual_queries: LLM returned empty response")
    return json.loads(response.text)

async def infer_designation_group(user_profile: str) -> dict:
    """
    Classify the designation into Group (AB/CD) and seniority tier using the same
    5-tier taxonomy used by the course seniority tagger.
    Returns dict with 'group' and 'seniority_tier'.
    """
    system_instruction = """You are an expert in Indian government service classification rules.
Given a civil servant role profile, classify the designation into:

1. group — one of:
   - AB: Group A or Group B — gazetted/senior officers, policymakers, managers, specialists
         (IAS, IPS, directors, deputy secretaries, section officers, engineers, doctors, scientists, etc.)
   - CD: Group C or Group D — supporting/clerical/operational staff
         (clerks, assistants, stenographers, drivers, MTS, helpers, data entry operators, technicians, constables, peons, etc.)

2. seniority_tier — one of these exact values:
   - "Entry Level"       → Probationers, LDCs, newly recruited staff, 0–3 yrs experience
   - "Junior Officer"    → Section Officers, Inspectors, field operational officers, 3–8 yrs
   - "Mid-Level Officer" → Under Secretary, Deputy Secretary, Director, 8–15 yrs
   - "Senior Officer"    → Joint Secretary, Additional Secretary, 15–25 yrs
   - "Apex / Leadership" → Secretary, DG, HoD, Cabinet Secretary, 25+ yrs

Use the designation name, responsibilities, and activities to reason before classifying.
Return ONLY a JSON object with both fields. No markdown."""

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
            "properties": {
                "group": {
                    "type": "STRING",
                    "enum": ["AB", "CD"]
                },
                "seniority_tier": {
                    "type": "STRING",
                    "enum": ["Entry Level", "Junior Officer", "Mid-Level Officer", "Senior Officer", "Apex / Leadership"]
                },
            },
            "required": ["group", "seniority_tier"],
        },
        system_instruction=[types.Part.from_text(text=DESIGNNATION_GROUP_SYSTEM_PROMPT)],
    )

    try:
        response = await client.aio.models.generate_content(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[user_part])],
            config=config,
        )
        if not response.text:
            logger.warning("Designation group LLM returned empty response, defaulting to AB/Mid-Level Officer")
            return {"group": "AB", "seniority_tier": "Mid-Level Officer"}
        result = json.loads(response.text)
        logger.info(f"LLM classified designation — group: {result.get('group')} | seniority: {result.get('seniority_tier')}")
        return result
    except Exception as e:
        logger.warning(f"Designation group inference failed, defaulting to AB/Mid-Level Officer: {e}")
        return {"group": "AB", "seniority_tier": "Mid-Level Officer"}

async def infer_seniority_tier(user_profile: str) -> str:
    """
    Classify the designation's seniority tier using the same 5-tier taxonomy
    used by the course seniority tagger. Returns the seniority tier string.
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
            "properties": {
                "seniority_tier": {
                    "type": "STRING",
                    "enum": ["Entry Level", "Junior Officer", "Mid-Level Officer", "Senior Officer", "Apex / Leadership"]
                },
            },
            "required": ["seniority_tier"],
        },
        system_instruction=[types.Part.from_text(text=SENIORITY_GROUP_SYSTEM_PROMPT)],
    )

    try:
        response = await client.aio.models.generate_content(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[user_part])],
            config=config,
        )
        if not response.text:
            logger.warning("Seniority tier LLM returned empty response, defaulting to Mid-Level Officer")
            return "Mid-Level Officer"
        result = json.loads(response.text)
        seniority_tier = result.get("seniority_tier", "Mid-Level Officer")
        logger.info(f"LLM classified seniority: {seniority_tier}")
        return seniority_tier
    except Exception as e:
        logger.warning(f"Seniority tier inference failed, defaulting to Mid-Level Officer: {e}")
        return "Mid-Level Officer"


async def get_filtered_courses_by_llm(
    courses_prompt: str,
    user_profile: str,
    organisation: str,
    user_seniority_tier: str = "Mid-Level Officer",
) -> str:
    """
    LLM-based course selection and scoring with:
    - Provider priority (own-org courses preferred)
    - Domain-mix enforcement
    - Sector-specific domain inclusion
    - Topic/type diversity within domain courses
    - Seniority-gated filtering using the same framework as the course tagger

    Returns a verdict for EVERY candidate course (selected or discarded, with a reason)
    so the decision is auditable per-course instead of only returning the winners.
    """
    logger.info(f"Filtering courses by LLM — seniority: {user_seniority_tier}")

    mix_rule = "Domain: ~45%, Behavioral: ~27%, Functional: ~28%"

    user_part = types.Part.from_text(text=f"""
Role Profile:
{user_profile}

Own Organisation: {organisation or 'N/A'}

Candidate Courses (Course ID | Name | Similarity Score | Organisation | Own Org | Competency Areas):
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
                "competency_type":   {"type": "STRING", "description": "Domain | Behavioral | Functional"},
                "rationale":         {"type": "STRING"},
                "seniority_tier":    {"type": "STRING", "description": "Tier/tier-range the course targets"},
                "seniority_match":   {"type": "BOOLEAN"},
                "decision":          {"type": "STRING", "enum": ["selected", "discarded"]},
                "discard_reason":    {
                    "type": "STRING",
                    "enum": ["none", "low_relevancy", "seniority_mismatch", "domain_mix_cap", "other"],
                },
            },
            "required": [
                "identifier", "relevancy", "competency_type", "seniority_tier",
                "seniority_match", "decision", "discard_reason",
            ],
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
        system_instruction=[types.Part.from_text(text=COURSE_SELECTION_SYSTEM_PROMPT.format(
            user_seniority_tier=user_seniority_tier, mix_rule=mix_rule,
        ))],
        thinking_config=types.ThinkingConfig(include_thoughts=False, thinking_budget=0),
    )

    response = await client.aio.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents=[types.Content(role="user", parts=[user_part])],
        config=config,
    )
    logger.info("LLM filtering completed")

    if not response.text:
        logger.exception(f"LLM filtering empty response — failed to inspect:")
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


async def process_recommendation_task(
    recommendation_id: uuid.UUID,
    user_profile: str,
    organisation: str,
    designation_name: str,
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
    logger.info(f"Background task started for recommendation_id: {recommendation_id}")

    try:
        # 1. Verify record exists
        rec_record = await crud_recommended_course.get_by_id(recommendation_id)
        if not rec_record:
            logger.error(f"Record {recommendation_id} not found in background task")
            return

        # 2. Generate 3 contextual queries + domain search keywords (single LLM call)
        queries = await generate_contextual_queries(user_profile)
        keyword_query     = queries.get("keyword_query", "")
        description_query = queries.get("description_query", "")
        combined_query    = queries.get("combined_query", "")
        search_keywords   = queries.get("search_keywords", [])
        logger.info(f"Queries generated — keyword: {keyword_query[:60]}... | pg_keywords: {search_keywords}")
        
        all_queries = [{
            "keyword_query": keyword_query,
            "description_query": description_query,
            "combined_query": combined_query,
            "search_keywords": search_keywords
        }]

        # Mechanically built competency taxonomy query (zero-cost, no LLM call) — used to
        # pre-filter and boost functional/behavioural courses in the candidate pool.
        competency_query = _build_competency_query(raw_competencies or [])

        # 3. Embed all queries in parallel
        kw_emb_list, desc_emb_list, comb_emb_list, comp_emb_list = await asyncio.gather(
            get_embedding(keyword_query),
            get_embedding(description_query),
            get_embedding(combined_query),
            get_embedding(competency_query),
        )
        if not kw_emb_list or not desc_emb_list or not comb_emb_list:
            raise Exception("Failed to generate one or more embeddings")

        kw_emb   = kw_emb_list[0].values
        desc_emb = desc_emb_list[0].values
        comb_emb = comb_emb_list[0].values
        comp_emb = comp_emb_list[0].values if comp_emb_list else None

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
            # Pre-filtered functional courses (competencyAreaName LIKE '%functional%')
            crud_recommended_course.fetch_competency_typed_courses(
                combined_emb=comp_emb or comb_emb,
                competency_type="functional",
                limit=40,
            ),
            # Pre-filtered behavioral courses (competencyAreaName LIKE '%behavioural%')
            crud_recommended_course.fetch_competency_typed_courses(
                combined_emb=comp_emb or comb_emb,
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

            comp_names = ""
            competencies_info = getattr(meta, "competencies_v6", None)
            if competencies_info:
                try:
                    comp_list = competencies_info if isinstance(competencies_info, list) else []
                    areas = list({e.get("competencyAreaName", "") for e in comp_list if e.get("competencyAreaName")})
                    if areas:
                        comp_names = f"Competency Areas: {', '.join(areas[:5])}"
                except Exception:
                    pass

            is_own_org = "YES" if (organisation and org_info and organisation.lower() in org_info.lower()) else "NO"

            desc_raw  = getattr(meta, "description", None) if meta else None
            desc_info = str(desc_raw).strip()[:500] if desc_raw else ""

            instr_raw  = getattr(meta, "instructions", None) if meta else None
            instr_info = re.sub(r'<[^>]+>', ' ', str(instr_raw)).strip()[:400] if instr_raw else ""

            candidate_lines.append(
                f"Course ID: {c['identifier']} | "
                f"Name: {c['name']} | "
                f"Similarity: {c['distance']:.4f} | "
                f"Organisation: {org_info or 'N/A'} | "
                f"Own Org: {is_own_org} | "
                f"Description: {desc_info} | "
                f"Learning Objectives: {instr_info} | "
                f"{comp_names}"
            )

        courses_prompt = "\n".join(candidate_lines)

        # 8. Determine seniority tier (LLM-reasoned, same taxonomy as course tagger)
        user_seniority_tier = await infer_seniority_tier(user_profile)
        logger.info(f"Designation classified — seniority: {user_seniority_tier}")

        # 9. LLM filtering + general courses (parallel)
        filtered_courses_json, general_courses = await asyncio.gather(
            get_filtered_courses_by_llm(courses_prompt, user_profile, organisation, user_seniority_tier),
            get_general_courses_from_gemini(user_profile),
        )

        # LLM now returns a verdict for every candidate (selected or discarded + reason),
        # not just the winners — needed to audit seniority filtering per course.
        id_to_name = {c["identifier"]: c["name"] for c in all_candidates}
        all_verdicts = json.loads(filtered_courses_json)
        for v in all_verdicts:
            v["course"] = id_to_name.get(v.get("identifier"), "")

        filtered_courses = [v for v in all_verdicts if v.get("decision") == "selected"]
        llm_filtered_snapshot = [
            {
                "identifier": c.get("identifier"),
                "name": c.get("course"),
                "relevancy": c.get("relevancy"),
                "competency_type": c.get("competency_type"),
                "seniority_tier": c.get("seniority_tier"),
            }
            for c in filtered_courses
        ]

        discard_reason_counts: Dict[str, int] = {}
        for v in all_verdicts:
            if v.get("decision") == "discarded":
                reason = v.get("discard_reason") or "other"
                discard_reason_counts[reason] = discard_reason_counts.get(reason, 0) + 1

        # 10. Enrich filtered courses with full metadata
        filtered_identifiers = [c["identifier"] for c in filtered_courses]
        if filtered_identifiers:
            f_identifiers_str = ", ".join(f"'{id}'" for id in filtered_identifiers)
            enriched_rows = await crud_recommended_course.fetch_course_metadata(f_identifiers_str)
            enriched_map = {row.identifier: row for row in enriched_rows}
        else:
            enriched_map = {}

        for course in filtered_courses:
            course["is_public"] = False
            meta = enriched_map.get(course["identifier"])
            if meta:
                course["competencies"] = meta.competencies_v6
                course["duration"] = meta.duration
                _org = meta.organisation
                course["organisation"] = (
                    ", ".join(str(o) for o in _org if o) if isinstance(_org, list) else (_org or None)
                )
            else:
                course["competencies"] = None
                course["duration"] = None
                course["organisation"] = None

        final_filtered_courses = filtered_courses + general_courses

        # Trace: dump course names/identifiers at every pipeline layer for debugging
        try:
            trace = {
                "recommendation_id": str(recommendation_id),
                "designation_name": designation_name,
                "organisation": organisation,
                "user_seniority_tier": user_seniority_tier,
                "queries": all_queries[0],
                "layers": {
                    "1_vector_search": [
                        {"identifier": identifier, "name": name, "score": float(score)}
                        for identifier, name, score in vector_results
                    ],
                    "2_keyword_search": [
                        {"identifier": identifier, "name": name, "score": float(score)}
                        for identifier, name, score in kw_results
                    ],
                    "3_merged_candidates": [
                        {"identifier": c["identifier"], "name": c["name"], "score": c["distance"]}
                        for c in all_candidates
                    ],
                    "4_llm_filtered_igot": llm_filtered_snapshot,
                    "5_general_public": [
                        {
                            "identifier": c.get("identifier"),
                            "name": c.get("course"),
                            "relevancy": c.get("relevancy"),
                            "platform": c.get("platform"),
                        }
                        for c in general_courses
                    ],
                    "6_final_combined": [
                        {"identifier": c.get("identifier"), "name": c.get("course"), "is_public": c.get("is_public")}
                        for c in final_filtered_courses
                    ],
                },
                "counts": {
                    "vector_search": len(vector_results),
                    "keyword_search": len(kw_results),
                    "merged_candidates": len(all_candidates),
                    "llm_filtered_igot": len(llm_filtered_snapshot),
                    "general_public": len(general_courses),
                    "final_combined": len(final_filtered_courses),
                },
                # Per-course seniority verdicts for every candidate (selected + discarded),
                # so seniority-driven drops are auditable instead of inferred.
                "seniority_analysis": {
                    "user_seniority_tier": user_seniority_tier,
                    "discarded_total": len(all_verdicts) - len(filtered_courses),
                    "discard_reason_counts": discard_reason_counts,
                    "courses": [
                        {
                            "identifier": v.get("identifier"),
                            "name": v.get("course"),
                            "seniority_tier": v.get("seniority_tier"),
                            "seniority_match": v.get("seniority_match"),
                            "decision": v.get("decision"),
                            "discard_reason": v.get("discard_reason"),
                        }
                        for v in all_verdicts
                    ],
                },
            }
            trace_dir = "logs/course_recommendation_trace"
            os.makedirs(trace_dir, exist_ok=True)
            with open(os.path.join(trace_dir, f"{recommendation_id}.json"), "w") as f:
                json.dump(trace, f, indent=2, ensure_ascii=False, default=str)
        except Exception:
            logger.warning(f"Failed to write course recommendation trace for {recommendation_id}", exc_info=True)

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
            f"{len(filtered_courses)} iGOT + {len(general_courses)} public courses"
        )

    except Exception as e:
        logger.exception(f"Course Recommendation background task failed for {recommendation_id}:")
        try:
            await crud_recommended_course.update_status_to_failed(recommendation_id, str(e))
        except Exception:
            logger.exception("CRITICAL: Failed to update status to FAILED:")

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
            role_mapping.designation_name or "",
            role_mapping.competencies or [],
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
