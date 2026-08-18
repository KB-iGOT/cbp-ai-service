import asyncio
import json
import uuid
from typing import Any, Dict, List
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

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
from ...services import llm_service

router = APIRouter(tags=["Course Recommendations"])


# ── LLM-backed helpers ────────────────────────────────────────────────────────
# These keep this module's own call surface; the prompt, response schema and generation
# config behind each one live in src/services/llm_service.py, so they work under any
# LLM_PROVIDER and nothing here has to know which model is answering.

async def get_embedding(text: str) -> list:
    """Embed one search query for course vector search.

    Returns the vector itself (a flat list of floats) — NOT a list of vectors. Blank input or
    a failed call yields [], which callers treat as "no vector for this query".
    """
    return await llm_service.embed_search_query(text)


async def generate_contextual_queries(user_profile: str) -> Dict[str, Any]:
    """
    Generate three contextually rich search queries + a keyword list from the user profile:
    - keyword_query     : compact phrase for keyword_embedding vector search
    - description_query : narrative paragraph for description_embedding vector search
    - combined_query    : multi-angle rich query for combined_embedding vector search
    - search_keywords   : list of 10-15 domain/skill terms for Postgres keyword search
    """
    return await llm_service.generate_contextual_queries(user_profile)


async def infer_designation_group(user_profile: str) -> str:
    """Classify the designation into Group A/B (senior/gazetted officers) or Group C/D
    (supporting/clerical staff). Returns 'AB' or 'CD', defaulting to 'AB' on failure."""
    return await llm_service.infer_designation_group(user_profile)


async def get_filtered_courses_by_llm(
    courses_prompt: str,
    user_profile: str,
    organisation: str,
    designation_group: str | None = None,
) -> str:
    """LLM-based course selection and scoring with:
    - Provider priority (own-org courses preferred)
    - Domain-mix enforcement by designation group
    - Sector-specific domain inclusion
    - Topic/type diversity within domain courses
    """
    return await llm_service.filter_courses(
        courses_prompt, user_profile, organisation, designation_group
    )


async def get_general_courses_from_gemini(user_profile) -> List[Dict[str, Any]]:
    """Fetch public courses from external learning platforms via provider web search.

    Disabled unless ENABLE_GENERAL_COURSE_LOOKUP is set, so this returns [] by default.
    """
    return await llm_service.fetch_general_courses(user_profile)

def _course_competency_areas(course: dict) -> set:
    """Return the set of competency areas a course covers, drawn from its competencies list
    (competencyAreaName / type): any of {'domain', 'functional', 'behavioural'}. A course can
    cover several. Tolerates both the British 'behavioural' and US 'behavioral' spellings, and
    mirrors the retrieval SQL (competencyAreaName LIKE '%functional%'/'%behavioural%') and the
    report grouping so 'a course covers type X' means the same thing everywhere."""
    areas = set()
    for c in course.get("competencies") or []:
        if not isinstance(c, dict):
            continue
        area = (c.get("competencyAreaName") or c.get("type") or "").lower()
        if "domain" in area:
            areas.add("domain")
        elif "function" in area:
            areas.add("functional")
        elif "behav" in area:
            areas.add("behavioural")
    return areas


def _passes_type_floor(course: dict) -> bool:
    """Relevancy floor. The lower Behavioural/Functional floor is applied ONLY to courses that
    carry no Domain competency — i.e. courses that can never appear in the Domain grouping. Any
    course that touches Domain (or covers no B/F area) keeps the original flat floor, so every
    Domain-contributing course is filtered exactly as it was before this change. This guarantees
    the Domain set is unchanged; only pure Behavioural/Functional courses are affected."""
    rel = course.get("relevancy", 0)
    areas = _course_competency_areas(course)
    if "domain" in areas or not (areas & {"functional", "behavioural"}):
        return rel >= settings.COURSE_RECOMMENDATION_MIN_RELEVANCY
    floors = []
    if "functional" in areas:
        floors.append(settings.FUNCTIONAL_MIN_RELEVANCY)
    if "behavioural" in areas:
        floors.append(settings.BEHAVIOURAL_MIN_RELEVANCY)
    return rel >= min(floors)


def _topup_relevancy(distance: float, floor: int) -> int:
    """Relevancy to record for a top-up course.

    A top-up never went through the LLM, so it has no LLM-assigned relevancy. Rather than stamping
    every top-up with the same floor constant (which made them indistinguishable and tied in the
    final sort), scale the retrieval similarity into a percentage and clamp it into [floor, 100]:
    the floor keeps them at or above the cutoff they were admitted under, so a downstream consumer
    filtering on `relevancy >= cutoff` still keeps them, while stronger retrieval matches now rank
    above weaker ones. Still an approximation, not an LLM judgement — `is_topup` marks it as such."""
    return max(floor, min(100, round(float(distance) * 100)))


def _enrich_topup_course(identifier: str, meta: Any, ptype: str, floor: int, distance: float) -> dict:
    """Build a course dict for a quota top-up candidate (never went through the LLM filter),
    shaped exactly like an LLM-filtered+enriched course (same keys, plus `is_topup`) so downstream
    persistence treats it identically to the others."""
    _org = getattr(meta, "organisation", None)
    return {
        "identifier": identifier,
        "course": meta.name,
        "relevancy": _topup_relevancy(distance, floor),
        "rationale": f"Added to meet minimum {ptype} competency coverage for this role.",
        "is_public": False,
        # Marks a deterministic quota top-up rather than an LLM-selected course, so the relevancy
        # above is read as a retrieval-similarity approximation, not an LLM relevance judgement.
        "is_topup": True,
        "competencies": meta.competencies_v6,
        "duration": meta.duration,
        "organisation": (
            ", ".join(str(o) for o in _org if o) if isinstance(_org, list) else (_org or None)
        ),
    }


def _enforce_competency_quotas(
    selected: List[dict],
    all_candidates: List[dict],
    metadata_map: Dict[str, Any],
) -> List[dict]:
    """Guarantee a minimum number of Behavioural, Functional AND Domain courses.

    For each type: count how many selected courses already cover that area; if under its minimum,
    top up the shortfall from the already-retrieved candidate pool, ranked by vector distance and
    deduped against the current selection, so identical input yields an identical set.

    Eligibility differs by type:
      - Behavioural / Functional → PURE candidates only (cover the type, carry NO Domain
        competency), so a B/F top-up can never enlarge the Domain grouping.
      - Domain → any Domain-bearing candidate.

    Domain has a minimum because it was observed swinging run-to-run (occasionally near zero) for
    the same role profile. This is a top-up ONLY: no course the LLM selected is ever dropped,
    trimmed, or reordered by this function — a type can freely exceed its minimum.

    Returns the adjusted list."""
    if not settings.ENFORCE_COMPETENCY_QUOTAS:
        return selected

    # B/F first so their deficits are measured against the LLM's own selection (unchanged
    # behaviour), then Domain — a Domain top-up that also covers B/F therefore cannot mask a
    # B/F shortfall. Domain uses the original flat relevancy cutoff, not a lower dedicated floor.
    reqs = {
        "behavioural": (settings.BEHAVIOURAL_MIN_COUNT, settings.BEHAVIOURAL_MIN_RELEVANCY),
        "functional":  (settings.FUNCTIONAL_MIN_COUNT,  settings.FUNCTIONAL_MIN_RELEVANCY),
        "domain":      (settings.DOMAIN_MIN_COUNT,      settings.COURSE_RECOMMENDATION_MIN_RELEVANCY),
    }

    result = list(selected)
    selected_ids = {c["identifier"] for c in result}

    def _is_eligible_topup(course: dict, ptype: str) -> bool:
        """True if the course may be used to top up ptype. Domain accepts any Domain-bearing
        course; Behavioural/Functional accept only PURE candidates (no Domain competency) so
        topping them up cannot enlarge the Domain grouping."""
        areas = _course_competency_areas(course)
        if ptype not in areas:
            return False
        if ptype == "domain":
            return True
        return "domain" not in areas

    for ptype, (min_count, floor) in reqs.items():
        covered = sum(1 for c in result if ptype in _course_competency_areas(c))
        deficit = min_count - covered
        if deficit <= 0:
            continue

        pool = sorted(
            (c for c in all_candidates
             if c.get("identifier") not in selected_ids and _is_eligible_topup(c, ptype)),
            key=lambda c: c.get("distance", 0),
            reverse=True,
        )
        added = 0
        for cand in pool:
            if added >= deficit:
                break
            meta = metadata_map.get(cand["identifier"])
            if not meta:
                continue
            result.append(
                _enrich_topup_course(
                    cand["identifier"], meta, ptype, floor, cand.get("distance", 0)
                )
            )
            selected_ids.add(cand["identifier"])
            added += 1

        if added < deficit:
            logger.warning(
                f"Quota: '{ptype}' still short by {deficit - added} after top-up (min {min_count}) "
                f"— no eligible {ptype} candidates left in the retrieved pool; likely a data gap."
            )
        else:
            logger.info(f"Quota: topped up {added} '{ptype}' course(s) to meet min {min_count}")

    return result


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
        
        # 3. Embed all queries in parallel. embed_search_query returns the vector itself (a
        #    flat list of floats), not a list of vectors — do not index into it.
        kw_emb, desc_emb, comb_emb, func_comp_emb, behav_comp_emb = await asyncio.gather(
            get_embedding(keyword_query),
            get_embedding(description_query),
            get_embedding(combined_query),
            get_embedding(functional_competency_query),
            get_embedding(behavioural_competency_query),
        )
        if not kw_emb or not desc_emb or not comb_emb:
            raise Exception("Failed to generate one or more embeddings")

        # These two are optional: a role with no functional/behavioural competencies yields an
        # empty query, hence an empty vector — fall back to the combined vector downstream.
        func_comp_emb = func_comp_emb or None
        behav_comp_emb = behav_comp_emb or None

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

        # 8. designation_group is intentionally not inferred: the B/F guarantee is enforced in code
        #    (pure-B/F top-up), not via prompt emphasis, so no per-group prompt tuning is needed and
        #    the LLM prompt stays identical to the original — keeping Domain selection unchanged.
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
            # Explicit False so the flag is present on every course, not only on quota top-ups:
            # this course's relevancy IS an LLM judgement (see _enrich_topup_course).
            course["is_topup"] = False
            meta = enriched_map.get(course["identifier"])
            if meta:
                course["course"] = meta.name
                course["competencies"] = meta.competencies_v6
                course["duration"] = meta.duration
                _org = meta.organisation
                course["organisation"] = (
                    ", ".join(str(o) for o in _org if o) if isinstance(_org, list) else (_org or None)
                )

        # Per-type relevancy floor: Domain-bearing (and untyped) courses keep the original flat
        # cutoff, so nothing Domain-bearing is filtered differently than before; only pure
        # Behavioural/Functional courses get the lower floor that stops them being silently deleted.
        floor_passed = [
            course for course in (filtered_courses + general_courses)
            if _passes_type_floor(course)
        ]

        # Guarantee a minimum count for Behavioural, Functional AND Domain via deterministic top-up
        # from the retrieved pool. Top-up only — no course the LLM selected is ever dropped or
        # reordered here. B/F top-ups stay restricted to pure B/F candidates so they cannot inflate
        # the Domain grouping; Domain tops up from any Domain-bearing candidate.
        final_filtered_courses = _enforce_competency_quotas(floor_passed, all_candidates, metadata_map)
        final_filtered_courses.sort(key=lambda course: course.get("relevancy", 0), reverse=True)

        _breakdown = {"domain": 0, "functional": 0, "behavioural": 0, "untyped": 0}
        for _c in final_filtered_courses:
            _areas = _course_competency_areas(_c)
            if not _areas:
                _breakdown["untyped"] += 1
            for _a in _areas:                       # a course can cover several areas
                _breakdown[_a] += 1
        logger.info(f"Final course competency coverage (courses may cover multiple): {_breakdown}")

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
            f"{len(final_filtered_courses)} courses after per-type floor + quota enforcement "
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
