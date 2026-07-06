import asyncio
import json
import os

from fastapi import APIRouter, Depends, HTTPException, status

from google import genai
from google.genai import types

from ...prompts.prompts import (
    CURRICULUM_COVERAGE_ANALYSIS_PROMPT,
    KCM_COMPETENCY_MATCHING_GUIDANCE,
    DOMAIN_COMPETENCY_MATCHING_GUIDANCE,
)
from ...models.user import User
from ...schemas.coverage_analysis import CoverageAnalysisRequest, CoverageAnalysisResponse

from ...core.logger import logger
from ...core.configs import settings

from ...api.dependencies import get_current_active_user

router = APIRouter(tags=["Curriculum Coverage Analysis"])

# KCM theme/sub-theme -> description lookup, used to enrich Behavioral/Functional course
# evidence with the same theme/sub-theme descriptions the role-mapping competencies were
# generated against. Domain competencies aren't drawn from this taxonomy, so this lookup is
# never used for Domain (see DOMAIN_COMPETENCY_MATCHING_GUIDANCE).
with open("data/competencies.json") as f:
    _KCM_COMPETENCIES = json.load(f)

_KCM_THEME_LOOKUP: dict = {}
for _entry in _KCM_COMPETENCIES:
    _theme_key = (_entry.get("theme") or "").strip().lower()
    _sub_theme_key = (_entry.get("sub_theme") or "").strip().lower()
    if _theme_key and _sub_theme_key:
        _KCM_THEME_LOOKUP[(_theme_key, _sub_theme_key)] = {
            "theme_description": _entry.get("theme_description", ""),
            "sub_theme_description": _entry.get("sub_theme_description", ""),
        }

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS
client = genai.Client(
    project=settings.GOOGLE_PROJECT_ID,
    location=settings.GOOOGLE_PROJECT_LOCATION_GLOBAL,
    vertexai=settings.GOOGLE_GENAI_USE_VERTEXAI
)

# Score -> bracket thresholds. Computed in code, never trusted from the LLM directly, so the
# textual bracket is always numerically consistent with the score (see _bracket_from_score).
FULLY_COVERED_SCORE_THRESHOLD = 70
PARTIALLY_COVERED_SCORE_THRESHOLD = 30

_COMPETENCY_RESULT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "competency": {"type": "STRING"},
        "theme": {"type": "STRING"},
        "sub_theme": {"type": "STRING"},
        "coverage": {"type": "STRING", "enum": ["Fully Covered", "Partially Covered", "Not Covered"]},
        "supporting_courses": {"type": "ARRAY", "items": {"type": "STRING"}},
        "reason": {"type": "STRING"},
    },
    "required": ["competency", "theme", "sub_theme", "coverage", "supporting_courses", "reason"],
}
_COURSE_SCORE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "course_identifier": {"type": "STRING"},
        "course_name": {"type": "STRING"},
        "coverage_score": {"type": "INTEGER"},
        "reason": {"type": "STRING"},
    },
    "required": ["course_identifier", "course_name", "coverage_score", "reason"],
}
_TYPE_ANALYSIS_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "competency_coverage": {"type": "ARRAY", "items": _COMPETENCY_RESULT_SCHEMA},
        "course_scores": {"type": "ARRAY", "items": _COURSE_SCORE_SCHEMA},
    },
    "required": ["competency_coverage", "course_scores"],
}


def _normalize_type(type_str: str) -> str:
    """'Behavioral'/'Behavioural' -> 'behavioral'; 'Functional' -> 'functional'; anything
    else (i.e. 'Domain') passes through lowercased. Mirrors the same normalization already
    used in crud/dashboard.py's gap-analysis SQL."""
    return (type_str or "").strip().lower().replace("behavioural", "behavioral")


def _bracket_from_score(score: int) -> str:
    if score >= FULLY_COVERED_SCORE_THRESHOLD:
        return "Fully Covered"
    if score >= PARTIALLY_COVERED_SCORE_THRESHOLD:
        return "Partially Covered"
    return "Not Covered"


def _format_designation_details(designation_name: str, department_name: str | None, organisation_name: str | None) -> str:
    return (
        f"Designation: {designation_name}\n"
        f"Department: {department_name or 'N/A'}\n"
        f"Organisation: {organisation_name or 'N/A'}"
    )


def _format_competency_framework(competencies: list) -> str:
    return "\n".join(
        f"- Type: {c.type} | Theme: {c.theme} | Sub-theme: {c.sub_theme}"
        for c in competencies
    )


def _format_competency_tags(course_competencies: list) -> str:
    """Renders a course's own competencies_v6 tags enriched with their KCM theme/sub-theme
    descriptions (looked up from data/competencies.json), so the LLM can judge alignment
    against the same taxonomy the role-mapping's Behavioral/Functional competencies were
    generated from. Falls back to the bare theme/sub-theme names if no KCM match is found."""
    if not course_competencies:
        return "N/A"
    lines = []
    for tag in course_competencies:
        theme = tag.get("competencyThemeName", "")
        sub_theme = tag.get("competencySubThemeName", "")
        kcm = _KCM_THEME_LOOKUP.get((theme.strip().lower(), sub_theme.strip().lower()))
        line = f"{tag.get('competencyAreaName', '')}: {theme} -> {sub_theme}"
        if kcm:
            line += f" (Theme meaning: {kcm['theme_description']} | Sub-theme meaning: {kcm['sub_theme_description']})"
        lines.append(line)
    return "; ".join(lines)


def _format_course_evidence(courses: list, include_competency_detail: bool) -> str:
    """Course metadata for comparison: title/description/keywords always, plus — for
    Behavioral/Functional courses only — the course's own competency theme/sub-theme tags
    enriched with KCM descriptions. Domain courses omit competency tags entirely (see
    DOMAIN_COMPETENCY_MATCHING_GUIDANCE — Domain competencies aren't KCM-drawn, so a course's
    tags aren't a meaningful signal there)."""
    if not courses:
        return "(no courses available)"
    blocks = []
    for course in courses:
        block = (
            f"Course: {course.name} (identifier: {course.identifier})\n"
            f"Description: {course.description or 'N/A'}\n"
            f"Keywords: {', '.join(course.keywords) if course.keywords else 'N/A'}"
        )
        if include_competency_detail:
            block += f"\nCompetency Theme/Sub-theme: {_format_competency_tags(course.competencies)}"
        blocks.append(block)
    return "\n\n".join(blocks)


async def _analyze_one_type(
    designation_name: str,
    department_name: str | None,
    organisation_name: str | None,
    competency_type_label: str,
    competencies: list,
    evidence_courses: list,
    matching_guidance: str,
    include_competency_detail: bool,
) -> dict:
    """Runs one Gemini call scoped to a single competency type. Returns empty results without
    an LLM call if there are no competencies of this type in the framework at all.

    include_competency_detail=True (Behavioral/Functional) adds each course's own competency
    theme/sub-theme tags + KCM descriptions to the evidence text; False (Domain) omits them
    entirely — title/description/keywords only.

    Returns {"competency_coverage": [...], "course_scores": [...]} where course_scores has
    coverage_bracket computed in code from coverage_score (see _bracket_from_score), not
    trusted from the LLM, so the label is always numerically consistent with the score.
    """
    if not competencies:
        return {"competency_coverage": [], "course_scores": []}

    course_metadata_fields = (
        "Course Name, Description, Keywords, and Competency Theme/Sub-theme (with KCM descriptions)"
        if include_competency_detail
        else "Course Name, Description, Keywords"
    )
    prompt_text = CURRICULUM_COVERAGE_ANALYSIS_PROMPT.format(
        competency_type=competency_type_label,
        designation_details=_format_designation_details(designation_name, department_name, organisation_name),
        competency_framework=_format_competency_framework(competencies),
        course_metadata_fields=course_metadata_fields,
        course_evidence=_format_course_evidence(evidence_courses, include_competency_detail),
        matching_guidance=matching_guidance,
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt_text)])]
    generate_content_config = types.GenerateContentConfig(
        temperature=0,
        top_p=1,
        safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
        ],
        response_mime_type="application/json",
        response_schema=_TYPE_ANALYSIS_RESPONSE_SCHEMA,
    )
    response = await client.aio.models.generate_content(
        model=settings.GEMINI_PRO_MODEL_NAME,
        contents=contents,
        config=generate_content_config,
    )
    if not response.text:
        raise Exception(f"Empty response from Gemini for {competency_type_label} competencies")
    parsed = json.loads(response.text)

    for cs in parsed.get("course_scores", []):
        cs["coverage_bracket"] = _bracket_from_score(int(cs["coverage_score"]))

    return parsed


@router.post("/coverage-analysis/analyze", response_model=CoverageAnalysisResponse)
async def analyze_curriculum_coverage(
    request: CoverageAnalysisRequest,
    current_user: User = Depends(get_current_active_user)
):
    """
    Analyze whether the supplied recommended courses adequately cover the supplied
    competency framework for a designation — analyzed SEPARATELY per competency type
    (Behavioral / Functional / Domain), each against its own appropriately-scoped evidence,
    not one shared pool for everything.

    Produces two complementary views per type:
      - competency-wise: for each competency, Fully/Partially/Not Covered + supporting courses
      - course-wise: for each evidence course, a 0-100 relevance score for that competency
        type + a bracket computed in code from fixed thresholds (FULLY_COVERED_SCORE_THRESHOLD
        / PARTIALLY_COVERED_SCORE_THRESHOLD above), not trusted from the LLM directly.

    Evidence partitioning: each recommended course already carries its own competency_type
    (Domain | Behavioral | Functional) — assigned by the LLM at course-selection time in
    get_filtered_courses_by_llm (api/v1/course_recommendation.py) and persisted unchanged on
    every entry in RecommendedCourse.filtered_courses. That field is authoritative: the
    Behavioral competency section is checked only against courses recommended as Behavioral,
    Functional only against Functional courses, Domain only against Domain courses. Course
    metadata used is never what decides pool membership — competency_type already fixes that.

    Metadata depth differs by type: Behavioral/Functional evidence includes title,
    description, keywords, AND the course's own competency theme/sub-theme tags enriched with
    their KCM descriptions (both sides share the same taxonomy, so tags are a meaningful
    signal there). Domain evidence includes title, description, and keywords only — no
    competency tags — since Domain competencies are LLM-generated per designation, not drawn
    from KCM, so a course's tags have no reliable relationship to them (see
    DOMAIN_COMPETENCY_MATCHING_GUIDANCE).

    Fully stateless: no DB access, no persistence — everything needed is supplied directly
    in the request body. Auth via the existing get_current_active_user dependency.
    """
    try:
        behavioral_competencies, functional_competencies, domain_competencies = [], [], []
        for c in request.competencies:
            norm = _normalize_type(c.type)
            if norm == "behavioral":
                behavioral_competencies.append(c)
            elif norm == "functional":
                functional_competencies.append(c)
            else:
                domain_competencies.append(c)

        behavioral_evidence = [c for c in request.courses if _normalize_type(c.competency_type) == "behavioral"]
        functional_evidence = [c for c in request.courses if _normalize_type(c.competency_type) == "functional"]
        domain_evidence = [c for c in request.courses if _normalize_type(c.competency_type) == "domain"]

        behavioural_analysis, functional_analysis, domain_analysis = await asyncio.gather(
            _analyze_one_type(
                request.designation_name, request.department_name, request.organisation_name,
                "Behavioral", behavioral_competencies, behavioral_evidence, KCM_COMPETENCY_MATCHING_GUIDANCE,
                include_competency_detail=True,
            ),
            _analyze_one_type(
                request.designation_name, request.department_name, request.organisation_name,
                "Functional", functional_competencies, functional_evidence, KCM_COMPETENCY_MATCHING_GUIDANCE,
                include_competency_detail=True,
            ),
            _analyze_one_type(
                request.designation_name, request.department_name, request.organisation_name,
                "Domain", domain_competencies, domain_evidence, DOMAIN_COMPETENCY_MATCHING_GUIDANCE,
                include_competency_detail=False,
            ),
        )

        return CoverageAnalysisResponse(
            behavioural=behavioural_analysis["competency_coverage"],
            functional=functional_analysis["competency_coverage"],
            domain=domain_analysis["competency_coverage"],
            behavioural_course_scores=behavioural_analysis["course_scores"],
            functional_course_scores=functional_analysis["course_scores"],
            domain_course_scores=domain_analysis["course_scores"],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Curriculum coverage analysis failed:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to analyze curriculum coverage: {str(e)}"
        )
