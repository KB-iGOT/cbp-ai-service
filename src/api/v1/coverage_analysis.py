import asyncio
import math
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from google import genai
from google.genai import types

from ...models.user import User
from ...schemas.coverage_analysis import CoverageAnalysisRequest, CoverageAnalysisResponse

from ...core.logger import logger
from ...core.configs import settings
from ...core.database import get_db_session

from ...api.dependencies import get_current_active_user
from ...crud.role_mapping import crud_role_mapping
from ...crud.course_recommendation import crud_recommended_course
from ...crud.coverage_analysis import crud_coverage_analysis

router = APIRouter(tags=["Curriculum Coverage Analysis"])

# Dedicated embedding client (api_key-based, not Vertex) — mirrors the pattern already used in
# api/v1/course_recommendation.py's get_embedding and services/designation_matcher_service.py.
_embedding_client = genai.Client(api_key=settings.GOOGLE_API_KEY, vertexai=False)

# Global cap on concurrent domain-explanation Gemini calls. Without this, concurrency stacks
# multiplicatively across a role mapping's domain competencies (each needs its own call) and
# across concurrent requests — this semaphore bounds the true bottleneck resource (total
# concurrent Gemini calls) regardless of how many competencies/requests are in flight.
_DOMAIN_EXPLANATION_SEM = asyncio.Semaphore(8)

_DOMAIN_MATCH_EXPLANATION_PROMPT = """A Domain competency requires:
Theme: {theme}
Sub-theme: {sub_theme}

Here are {n} recommended Domain courses being considered for this competency:

{courses_block}

For EACH course listed above, on its own line, estimate what PERCENTAGE (0-100%) of this
competency's theme/sub-theme content that course actually covers, based on its own
description/keywords — then explain why in one concise sentence, referencing actual
concepts/keywords from the course, not a generic restatement of the theme. Judge coverage
purely on substance.

Format EXACTLY as one line per course, in the same order given above:
<course name> covers <percentage>% because <explanation>
"""


async def _explain_domain_matches(theme: str, sub_theme: str, all_courses: list) -> str:
    """all_courses: EVERY domain course scored for this competency (list of {"course_name",
    "score", "description", "keywords"} dicts, best first — not just the winner). ONE call
    covers all of them (typically 10-15 courses) — bounded by _DOMAIN_EXPLANATION_SEM at the
    call site, not by splitting the prompt itself. The similarity score is intentionally NOT
    passed into the prompt — it would anchor the model's percentage estimate; coverage is
    judged purely on the course's own description/keywords against the theme/sub-theme."""
    if not all_courses:
        return "N/A — no domain courses to match against."
    courses_block = "\n\n".join(
        f"{i}. Course: {c['course_name']}\n"
        f"   Description: {c.get('description') or 'N/A'}\n"
        f"   Keywords: {', '.join(c.get('keywords') or []) or 'N/A'}"
        for i, c in enumerate(all_courses, start=1)
    )
    try:
        async with _DOMAIN_EXPLANATION_SEM:
            response = await _embedding_client.aio.models.generate_content(
                model=settings.GEMINI_FLASH_MODEL_NAME,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=_DOMAIN_MATCH_EXPLANATION_PROMPT.format(
                    theme=theme, sub_theme=sub_theme, n=len(all_courses), courses_block=courses_block,
                ))])],
                config=types.GenerateContentConfig(temperature=0),
            )
        return (response.text or "").strip()
    except Exception as e:
        return f"(explanation generation failed: {e})"


def _normalize_type(type_str: str) -> str:
    """'Behavioral'/'Behavioural' -> 'behavioral'; 'Functional' -> 'functional'; anything
    else (i.e. 'Domain') passes through lowercased. Mirrors the same normalization already
    used in crud/dashboard.py's gap-analysis SQL."""
    return (type_str or "").strip().lower().replace("behavioural", "behavioral")


def _normalize_text(value: str | None) -> str:
    return (value or "").strip().lower()


def _bf_competency_coverage(competencies: list, courses: list, type_key: str, area_label: str) -> list:
    """Behavioral/Functional coverage: exact (theme, sub_theme) pair match between a
    competency and a course's own KCM tag. `courses` is the FULL recommended-course list
    (not pre-filtered) — filtered_courses entries carry no top-level competency_type field,
    so each tag's own competencyAreaName (restricted to type_key here) is the ground truth
    for which tags are even eligible to match. A course tagged with the same sub_theme under
    a different theme (or vice versa) does NOT count — both fields must match exactly."""
    rows = []
    for comp in competencies:
        theme = comp.get("theme", "")
        sub_theme = comp.get("sub_theme", "")
        comp_theme = _normalize_text(theme)
        comp_sub_theme = _normalize_text(sub_theme)
        supporting = []
        for course in courses:
            for tag in (course.get("competencies") or []):
                if _normalize_type(tag.get("competencyAreaName")) != type_key:
                    continue
                if (
                    _normalize_text(tag.get("competencyThemeName")) == comp_theme
                    and _normalize_text(tag.get("competencySubThemeName")) == comp_sub_theme
                ):
                    supporting.append(course.get("course", ""))
                    break
        matched = bool(supporting)
        rows.append({
            "competency_area": area_label,
            "theme": theme,
            "sub_theme": sub_theme,
            "competency": f"{theme} - {sub_theme}",
            "matched": matched,
            "match_type": "exact_tag_match",
            "matching_score": None,
            "matched_courses": supporting,
            "course_scores": [],
            "rationale": (
                f"Exact theme/sub-theme tag match found in {len(supporting)} recommended course(s)."
                if matched
                else "No recommended course carries this exact theme/sub-theme tag."
            ),
        })
    return rows


def _format_for_embedding(text: str) -> str:
    return f"task: sentence similarity | query: {text}"


async def _get_embeddings(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    contents = [_format_for_embedding(t) for t in texts]
    response = await _embedding_client.aio.models.embed_content(
        model=settings.GOOGLE_EMBEDDING_MODEL,
        contents=contents,
        config=types.EmbedContentConfig(output_dimensionality=settings.EMBEDDING_OUTPUT_DIMENSIONALITY),
    )
    return [e.values for e in response.embeddings]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


async def _domain_competency_coverage(competencies: list, evidence_courses: list) -> list:
    """Domain coverage: semantic similarity between a competency's theme/sub_theme text and
    each Domain course's title+keywords+description, via embedding cosine similarity. No
    threshold is applied — every domain course is reported with its score (sorted high→low)
    in course_scores, matching_score is the best score, and matched is left null so the caller
    decides what counts. Domain competencies aren't drawn from the KCM taxonomy, so a course's
    tags have no reliable relationship to them — hence semantic matching, not tag matching."""
    if not competencies:
        return []

    if not evidence_courses:
        return [
            {
                "competency_area": "Domain",
                "theme": c.get("theme", ""),
                "sub_theme": c.get("sub_theme", ""),
                "competency": f"{c.get('theme', '')} - {c.get('sub_theme', '')}",
                "matched": None,
                "match_type": "semantic_similarity",
                "matching_score": None,
                "matched_courses": [],
                "course_scores": [],
                "rationale": "No Domain courses recommended to match against.",
            }
            for c in competencies
        ]

    competency_texts = [f"{c.get('theme', '')} {c.get('sub_theme', '')}".strip() for c in competencies]
    course_texts = [
        f"{c['name']} {' '.join(c.get('keywords') or [])} {c.get('description') or ''}".strip()
        for c in evidence_courses
    ]
    competency_embeddings, course_embeddings = await asyncio.gather(
        _get_embeddings(competency_texts),
        _get_embeddings(course_texts),
    )

    rows = []
    for comp, comp_emb in zip(competencies, competency_embeddings):
        scored = [
            {"course_name": course["name"], "score": round(_cosine_similarity(comp_emb, course_emb), 4)}
            for course, course_emb in zip(evidence_courses, course_embeddings)
        ]
        scored.sort(key=lambda s: s["score"], reverse=True)
        best = scored[0] if scored else None
        theme = comp.get("theme", "")
        sub_theme = comp.get("sub_theme", "")
        rows.append({
            "competency_area": "Domain",
            "theme": theme,
            "sub_theme": sub_theme,
            "competency": f"{theme} - {sub_theme}",
            "matched": None,
            "match_type": "semantic_similarity",
            "matching_score": best["score"] if best else None,
            "matched_courses": [best["course_name"]] if best else [],
            "course_scores": scored,
            "rationale": (
                f"Best semantic match {best['score']} against '{best['course_name']}' "
                f"(all {len(scored)} domain courses scored below)."
                if best
                else "No Domain courses recommended to match against."
            ),
        })
    return rows


def _log_coverage_summary(role_mapping_id: uuid.UUID, area_label: str, rows: list) -> None:
    matched = sum(1 for r in rows if r.get("matched") is True)
    logger.info(
        f"[coverage-analysis][{role_mapping_id}] {area_label}: {len(rows)} competencies "
        f"(exact-matched={matched})" if rows and rows[0].get("match_type") == "exact_tag_match"
        else f"[coverage-analysis][{role_mapping_id}] {area_label}: {len(rows)} competencies (semantic, no threshold)"
    )
    for r in rows:
        score = r.get("matching_score")
        logger.info(
            f"[coverage-analysis][{role_mapping_id}] {area_label} | {r['theme']} - {r['sub_theme']} "
            f"| matched={r.get('matched')} score={score} | top_courses={r['matched_courses']}"
        )


@router.post("/coverage-analysis/analyze", response_model=CoverageAnalysisResponse)
async def analyze_curriculum_coverage(
    request: CoverageAnalysisRequest,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
):
    """
    Analyze whether a designation's recommended courses cover its competency framework —
    per competency type (Behavioral / Functional / Domain). Fully deterministic, no LLM call:

      - Behavioral/Functional: a competency is matched only if at least one recommended course
        carries the EXACT same (theme, sub_theme) pair as its own KCM tag (see
        _bf_competency_coverage). Course category is read from each course's own tags
        (competencies[].competencyAreaName) — filtered_courses has no top-level competency_type.
      - Domain: embedding cosine similarity between the competency's theme/sub_theme and each
        Domain course's title+keywords+description. NO threshold — every course is reported
        with its score; the caller decides what counts (see _domain_competency_coverage).

    Data is fetched server-side from `request.role_mapping_id`, scoped to the current user
    (competency framework from role_mappings.competencies; courses from
    RecommendedCourse.filtered_courses; Domain course description/keywords from
    course_metadata_weightage). The result is upserted into coverage_analysis_results (one row
    per role_mapping_id+user_id) — see GET /coverage-analysis/{role_mapping_id}.

    Response is a flat `rows` list (one row per competency) plus designation/role-mapping
    metadata, so it converts directly into a table.
    """
    try:
        role_mapping = await crud_role_mapping.get_by_id_and_user(
            db, request.role_mapping_id, current_user.user_id
        )
        if not role_mapping:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found or access denied",
            )

        recommended_course = await crud_recommended_course.get_by_role_mapping_id(
            db, request.role_mapping_id, current_user.user_id
        )
        filtered_courses = recommended_course.filtered_courses if recommended_course else []

        competencies = role_mapping.competencies or []
        behavioral_competencies, functional_competencies, domain_competencies = [], [], []
        for c in competencies:
            norm = _normalize_type(c.get("type"))
            if norm == "behavioral":
                behavioral_competencies.append(c)
            elif norm == "functional":
                functional_competencies.append(c)
            else:
                domain_competencies.append(c)

        # filtered_courses entries carry NO top-level competency_type — each course's own tag
        # list (competencies[].competencyAreaName) is the only per-course category signal.
        def _has_tag_of_type(course: dict, type_key: str) -> bool:
            return any(
                _normalize_type(tag.get("competencyAreaName")) == type_key
                for tag in (course.get("competencies") or [])
            )

        domain_raw = [c for c in filtered_courses if _has_tag_of_type(c, "domain")]

        logger.info(
            f"[coverage-analysis][{request.role_mapping_id}] total_recommended_courses={len(filtered_courses)} "
            f"behavioral_tagged={sum(1 for c in filtered_courses if _has_tag_of_type(c, 'behavioral'))} "
            f"functional_tagged={sum(1 for c in filtered_courses if _has_tag_of_type(c, 'functional'))} "
            f"domain_tagged={len(domain_raw)}"
        )

        # Domain evidence needs description/keywords, which filtered_courses doesn't carry —
        # fetch it from course_metadata_weightage, same as recommendation-time enrichment does.
        domain_metadata_map = {}
        domain_identifiers = [c["identifier"] for c in domain_raw]
        if domain_identifiers:
            identifiers_str = ", ".join(f"'{i}'" for i in domain_identifiers)
            rows = await crud_recommended_course.fetch_course_metadata(identifiers_str)
            domain_metadata_map = {row.identifier: row for row in rows}

        domain_evidence = []
        for c in domain_raw:
            meta = domain_metadata_map.get(c["identifier"])
            domain_evidence.append({
                "identifier": c["identifier"],
                "name": c.get("course") or (meta.name if meta else ""),
                "description": meta.description if meta else None,
                "keywords": meta.keywords if meta else None,
            })

        behavioural_rows = _bf_competency_coverage(behavioral_competencies, filtered_courses, "behavioral", "Behavioral")
        functional_rows = _bf_competency_coverage(functional_competencies, filtered_courses, "functional", "Functional")
        domain_rows = await _domain_competency_coverage(domain_competencies, domain_evidence)

        # Rationale rewrite: _bf_competency_coverage only reports the matched COUNT ("found in
        # 2 recommended course(s)"), not what it's a fraction OF. Rewrite to "2/5 course(s)"
        # using the total Behavioral/Functional evidence pool size — doesn't touch
        # _bf_competency_coverage's own matched/matched_courses semantics, just the display text.
        behavioral_pool_size = sum(1 for c in filtered_courses if _has_tag_of_type(c, "behavioral"))
        functional_pool_size = sum(1 for c in filtered_courses if _has_tag_of_type(c, "functional"))
        for row, pool_size in (
            [(r, behavioral_pool_size) for r in behavioural_rows]
            + [(r, functional_pool_size) for r in functional_rows]
        ):
            matched_count = len(row["matched_courses"])
            row["rationale"] = (
                f"Exact theme/sub-theme tag match found in {matched_count}/{pool_size} course(s)."
                if matched_count
                else f"No recommended course carries this exact theme/sub-theme tag (0/{pool_size} course(s))."
            )

        # Domain elaboration: for EVERY scored Domain course (not just the single best), one LLM
        # call explains all of them together against the theme/sub-theme, judged on substance
        # (description/keywords) rather than the raw similarity score. Parallelized across this
        # role mapping's domain competencies, bounded globally by _DOMAIN_EXPLANATION_SEM.
        domain_evidence_by_name = {c["name"]: c for c in domain_evidence}

        async def _build_domain_rationale(row: dict) -> str:
            all_courses = []
            for cs in (row.get("course_scores") or []):
                evidence = domain_evidence_by_name.get(cs["course_name"], {})
                all_courses.append({
                    "course_name": cs["course_name"],
                    "score": cs["score"],
                    "description": evidence.get("description"),
                    "keywords": evidence.get("keywords"),
                })
            return await _explain_domain_matches(row["theme"], row["sub_theme"], all_courses)

        domain_rationales = await asyncio.gather(*[_build_domain_rationale(row) for row in domain_rows])
        for row, rationale in zip(domain_rows, domain_rationales):
            row["rationale"] = rationale

        _log_coverage_summary(request.role_mapping_id, "Behavioral", behavioural_rows)
        _log_coverage_summary(request.role_mapping_id, "Functional", functional_rows)
        _log_coverage_summary(request.role_mapping_id, "Domain", domain_rows)

        await crud_coverage_analysis.upsert(
            db, request.role_mapping_id, current_user.user_id,
            behavioural=behavioural_rows,
            functional=functional_rows,
            domain=domain_rows,
        )

        return CoverageAnalysisResponse(
            designation_name=role_mapping.designation_name,
            role_mapping_id=str(request.role_mapping_id),
            state_center_name=role_mapping.state_center_name,
            department_name=role_mapping.department_name,
            rows=behavioural_rows + functional_rows + domain_rows,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Curriculum coverage analysis failed:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to analyze curriculum coverage: {str(e)}"
        )


@router.get("/coverage-analysis/{role_mapping_id}", response_model=CoverageAnalysisResponse)
async def get_curriculum_coverage(
    role_mapping_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
):
    """Fetch the last computed coverage analysis for this role mapping without recomputing —
    404 if /coverage-analysis/analyze has never been run for it (or the role mapping doesn't
    exist / doesn't belong to the current user)."""
    role_mapping = await crud_role_mapping.get_by_id_and_user(db, role_mapping_id, current_user.user_id)
    if not role_mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role mapping not found or access denied",
        )

    saved = await crud_coverage_analysis.get_by_role_mapping_id(db, role_mapping_id, current_user.user_id)
    if not saved:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No coverage analysis has been run for this role mapping yet",
        )

    return CoverageAnalysisResponse(
        designation_name=role_mapping.designation_name,
        role_mapping_id=str(role_mapping_id),
        state_center_name=role_mapping.state_center_name,
        department_name=role_mapping.department_name,
        rows=(saved.behavioural or []) + (saved.functional or []) + (saved.domain or []),
    )
