"""Tests for chunked CBP/ACBP report rendering and the report download handlers
in src/api/v1/reports.py. Database access and PDF conversion are mocked."""
import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api.v1 import reports
from src.api.v1.reports import ReportType, report_service
from src.core.configs import Settings, settings

HEADER = "Comprehensive Role-Based Competency Analysis"
SECTION_HEADING = "Detailed Competency Framework by"
FOOTER = "All Rights Reserved"
FAKE_PDF = b"%PDF-1.4 fake report"
BOTH_REPORTS = pytest.mark.parametrize("template_type", [ReportType.CBP, ReportType.ACBP])


def make_designation(index, with_course=False):
    """A role mapping with one unique competency of each type and, optionally, one course."""
    course = {
        "course": f"Course-{index:03d}",
        "organisation": ["Sample Institute"],
        "course_level": "Tactical",
        "relevancy": 90,
        "is_public": False,
        "competencies": [
            {"competencyAreaName": "Functional", "competencyThemeName": "Planning", "competencySubThemeName": "Budgeting"},
        ],
    }
    return SimpleNamespace(
        designation_name=f"Desig-{index:03d}",
        wing_division_section="Urban Transport Wing",
        department_name="Housing Department",
        state_center_name="Central Ministry",
        role_responsibilities=["Coordinate scheme implementation"],
        activities=["Review progress reports"],
        competencies=[
            {"type": "Behavioral", "theme": "Communication", "sub_theme": f"Listening {index}",
             "proficiency_level": "Tactical", "delivery_mode": "Online"},
            {"type": "Functional", "theme": "Finance", "sub_theme": f"Budgeting {index}",
             "proficiency_level": "Operational", "delivery_mode": "Blended"},
            {"type": "Domain", "theme": "Urban Development", "sub_theme": f"Housing {index}",
             "delivery_mode": "Offline"},
        ],
        cbp_plans=[SimpleNamespace(selected_courses=[course])] if with_course else [],
    )


def make_designations(count, with_course=False):
    return [make_designation(i, with_course) for i in range(1, count + 1)]


def current_user():
    return SimpleNamespace(user_id=uuid.uuid4())


async def read_body(response):
    return b"".join([chunk async for chunk in response.body_iterator])


@pytest.fixture
def chunk_size(monkeypatch):
    """Use small chunks so multi-chunk behaviour can be tested with a few designations."""
    monkeypatch.setattr(settings, "REPORT_PDF_DESIGNATIONS_PER_CHUNK", 2)
    return 2


@pytest.fixture
def convert(monkeypatch):
    mock = AsyncMock(return_value=FAKE_PDF)
    monkeypatch.setattr(reports, "convert_html_chunks_to_pdf", mock)
    return mock


@pytest.fixture
def english_only(monkeypatch):
    monkeypatch.setattr(reports.translation_service, "supported_languages", ["en", "hi"])


def test_chunking_settings_defaults():
    assert Settings.model_fields["REPORT_PDF_DESIGNATIONS_PER_CHUNK"].default == 50
    assert Settings.model_fields["REPORT_PDF_MAX_CONCURRENT_PAGES"].default == 4


# --- render_cbp_template_chunks -----------------------------------------------------------

@BOTH_REPORTS
@pytest.mark.usefixtures("chunk_size")
def test_designations_are_split_into_ordered_chunks(template_type):
    chunks = report_service.render_cbp_template_chunks(make_designations(5), "Housing Department", template_type)

    expected = [["Desig-001", "Desig-002"], ["Desig-003", "Desig-004"], ["Desig-005"]]
    assert len(chunks) == len(expected)
    all_names = [name for group in expected for name in group]
    for chunk, names in zip(chunks, expected):
        assert [name for name in all_names if name in chunk] == names


@BOTH_REPORTS
@pytest.mark.usefixtures("chunk_size")
def test_header_and_summary_render_only_in_first_chunk(template_type):
    chunks = report_service.render_cbp_template_chunks(make_designations(5), "Housing Department", template_type)

    assert [HEADER in chunk for chunk in chunks] == [True, False, False]
    assert [SECTION_HEADING in chunk for chunk in chunks] == [True, False, False]
    assert [chunk.count("Housing Department - Capacity Building Plan") for chunk in chunks] == [1, 0, 0]


@BOTH_REPORTS
@pytest.mark.usefixtures("chunk_size")
def test_footer_renders_only_in_last_chunk(template_type):
    chunks = report_service.render_cbp_template_chunks(make_designations(5), "Housing Department", template_type)

    assert [FOOTER in chunk for chunk in chunks] == [False, False, True]


@BOTH_REPORTS
@pytest.mark.usefixtures("chunk_size")
def test_competency_totals_cover_all_designations_not_one_chunk(template_type):
    chunks = report_service.render_cbp_template_chunks(make_designations(5), "Housing Department", template_type)

    # 5 designations x 3 unique competencies each; the first chunk alone holds only 6
    assert "Total: 15" in chunks[0]


@BOTH_REPORTS
@pytest.mark.usefixtures("chunk_size")
def test_designations_up_to_chunk_size_render_as_one_complete_document(template_type):
    chunks = report_service.render_cbp_template_chunks(make_designations(2), "Housing Department", template_type)

    assert len(chunks) == 1
    assert HEADER in chunks[0] and FOOTER in chunks[0]
    assert "Desig-001" in chunks[0] and "Desig-002" in chunks[0]


@BOTH_REPORTS
@pytest.mark.usefixtures("chunk_size")
def test_one_designation_over_chunk_size_starts_a_new_chunk(template_type):
    chunks = report_service.render_cbp_template_chunks(make_designations(3), "Housing Department", template_type)

    assert len(chunks) == 2
    assert "Desig-003" in chunks[1] and "Desig-001" not in chunks[1]
    assert FOOTER in chunks[1] and HEADER not in chunks[1]


@BOTH_REPORTS
def test_no_designations_still_renders_header_and_footer(template_type):
    chunks = report_service.render_cbp_template_chunks([], "Housing Department", template_type)

    assert len(chunks) == 1
    assert HEADER in chunks[0] and FOOTER in chunks[0]


@BOTH_REPORTS
@pytest.mark.usefixtures("chunk_size")
def test_language_applies_to_every_chunk(template_type):
    chunks = report_service.render_cbp_template_chunks(make_designations(5), "Housing Department", template_type, "hi")

    assert all('<html lang="hi">' in chunk for chunk in chunks)


@pytest.mark.usefixtures("chunk_size")
def test_acbp_courses_render_with_their_designation():
    chunks = report_service.render_cbp_template_chunks(
        make_designations(4, with_course=True), "Housing Department", ReportType.ACBP
    )

    assert ["Course-001" in chunks[0], "Course-002" in chunks[0]] == [True, True]
    assert ["Course-003" in chunks[1], "Course-004" in chunks[1]] == [True, True]
    assert "Course-003" not in chunks[0]


def test_default_chunk_size_keeps_fifty_designations_together():
    chunks = report_service.render_cbp_template_chunks(make_designations(51), "Housing Department", ReportType.CBP)

    assert len(chunks) == 2
    assert "Desig-050" in chunks[0] and "Desig-051" in chunks[1]


# --- generate_pdf_response ------------------------------------------------------------------

def test_generate_pdf_response_converts_all_chunks_into_one_attachment(convert):
    response = asyncio.run(reports.generate_pdf_response(["<html>1</html>", "<html>2</html>"], "ACBP_Report", "SC1"))

    convert.assert_awaited_once_with(["<html>1</html>", "<html>2</html>"])
    assert response.media_type == "application/pdf"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=ACBP_Report_SC1_")
    assert disposition.endswith(".pdf")
    assert asyncio.run(read_body(response)) == FAKE_PDF


# --- download_plan_report -----------------------------------------------------------------

@pytest.mark.usefixtures("chunk_size", "english_only")
def test_download_plan_report_renders_chunks_and_returns_pdf(monkeypatch, convert):
    fetch = AsyncMock(return_value=make_designations(5, with_course=True))
    monkeypatch.setattr(reports.crud_role_mapping, "get_all_completed_mapping", fetch)
    user = current_user()

    response = asyncio.run(reports.download_plan_report(
        state_center_id="SC1", report_type=ReportType.ACBP, report_name="ACBP",
        department_id="DEPT1", load_cbp_plans=True, db=None, current_user=user,
    ))

    fetch.assert_awaited_once_with(None, "SC1", user.user_id, "DEPT1", load_cbp_plans=True)
    chunks = convert.await_args.args[0]
    assert len(chunks) == 3
    assert "Housing Department - Capacity Building Plan" in chunks[0]
    assert response.headers["content-disposition"].startswith("attachment; filename=ACBP_Report_SC1_")
    assert asyncio.run(read_body(response)) == FAKE_PDF


@pytest.mark.usefixtures("english_only")
def test_download_plan_report_uses_state_name_without_department(monkeypatch, convert):
    monkeypatch.setattr(
        reports.crud_role_mapping, "get_all_completed_mapping", AsyncMock(return_value=make_designations(1))
    )

    asyncio.run(reports.download_plan_report(
        state_center_id="SC1", report_type=ReportType.CBP, report_name="CBP", db=None, current_user=current_user(),
    ))

    assert "Central Ministry - Capacity Building Plan" in convert.await_args.args[0][0]


@pytest.mark.usefixtures("english_only")
def test_download_plan_report_rejects_unsupported_language(convert):
    with pytest.raises(HTTPException) as error:
        asyncio.run(reports.download_plan_report(
            state_center_id="SC1", report_type=ReportType.CBP, report_name="CBP",
            language="xx", db=None, current_user=current_user(),
        ))

    assert error.value.status_code == 400
    convert.assert_not_awaited()


@pytest.mark.usefixtures("english_only")
def test_download_plan_report_returns_404_when_no_role_mappings(monkeypatch, convert):
    monkeypatch.setattr(reports.crud_role_mapping, "get_all_completed_mapping", AsyncMock(return_value=[]))

    with pytest.raises(HTTPException) as error:
        asyncio.run(reports.download_plan_report(
            state_center_id="SC1", report_type=ReportType.CBP, report_name="CBP", db=None, current_user=current_user(),
        ))

    assert error.value.status_code == 404
    convert.assert_not_awaited()


@pytest.mark.usefixtures("english_only")
def test_download_plan_report_passes_pdf_errors_through(monkeypatch):
    monkeypatch.setattr(
        reports.crud_role_mapping, "get_all_completed_mapping", AsyncMock(return_value=make_designations(1))
    )
    pdf_error = HTTPException(status_code=500, detail="Error generating PDF with Playwright")
    monkeypatch.setattr(reports, "convert_html_chunks_to_pdf", AsyncMock(side_effect=pdf_error))

    with pytest.raises(HTTPException) as error:
        asyncio.run(reports.download_plan_report(
            state_center_id="SC1", report_type=ReportType.ACBP, report_name="ACBP", db=None, current_user=current_user(),
        ))

    assert error.value is pdf_error


@pytest.mark.usefixtures("english_only")
def test_download_plan_report_wraps_unexpected_errors_as_500(monkeypatch):
    monkeypatch.setattr(
        reports.crud_role_mapping, "get_all_completed_mapping", AsyncMock(side_effect=RuntimeError("db down"))
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(reports.download_plan_report(
            state_center_id="SC1", report_type=ReportType.ACBP, report_name="ACBP", db=None, current_user=current_user(),
        ))

    assert error.value.status_code == 500
    assert error.value.detail == "Error generating PDF report for acbp plan"


@pytest.mark.parametrize(
    ("endpoint", "report_type", "report_name", "load_cbp_plans"),
    [
        (reports.download_cbp_plan, ReportType.CBP, "CBP", False),
        (reports.download_acbp_plan, ReportType.ACBP, "ACBP", True),
    ],
)
def test_download_endpoints_delegate_with_their_report_type(
    monkeypatch, endpoint, report_type, report_name, load_cbp_plans
):
    handler = AsyncMock(return_value="response")
    monkeypatch.setattr(reports, "download_plan_report", handler)
    user = current_user()

    result = asyncio.run(endpoint(state_center_id="SC1", department_id="DEPT1", language="en", db=None, current_user=user))

    assert result == "response"
    handler.assert_awaited_once_with(
        state_center_id="SC1", report_type=report_type, report_name=report_name, language="en",
        department_id="DEPT1", load_cbp_plans=load_cbp_plans, db=None, current_user=user,
    )


# --- download_course_recommendation_report --------------------------------------------------

def test_course_recommendation_report_is_converted_as_a_single_chunk(monkeypatch, convert):
    plan = SimpleNamespace(selected_courses=make_designation(1, with_course=True).cbp_plans[0].selected_courses)
    monkeypatch.setattr(reports.crud_cbp_plan, "get_by_role_mapping", AsyncMock(return_value=plan))
    monkeypatch.setattr(reports.crud_role_mapping, "get_by_id", AsyncMock(return_value=make_designation(1)))
    role_mapping_id = uuid.uuid4()

    response = asyncio.run(reports.download_course_recommendation_report(role_mapping_id, db=None, current_user=current_user()))

    chunks = convert.await_args.args[0]
    assert len(chunks) == 1
    assert "Course-001" in chunks[0]
    assert response.headers["content-disposition"].startswith(f"attachment; filename=Course_Report_{role_mapping_id}_")


def test_course_recommendation_report_returns_404_without_a_plan(monkeypatch, convert):
    monkeypatch.setattr(reports.crud_cbp_plan, "get_by_role_mapping", AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as error:
        asyncio.run(reports.download_course_recommendation_report(uuid.uuid4(), db=None, current_user=current_user()))

    assert error.value.status_code == 404
    convert.assert_not_awaited()


def test_course_recommendation_report_wraps_unexpected_errors_as_500(monkeypatch):
    monkeypatch.setattr(reports.crud_cbp_plan, "get_by_role_mapping", AsyncMock(side_effect=RuntimeError("db down")))

    with pytest.raises(HTTPException) as error:
        asyncio.run(reports.download_course_recommendation_report(uuid.uuid4(), db=None, current_user=current_user()))

    assert error.value.status_code == 500
