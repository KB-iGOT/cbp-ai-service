import asyncio
from datetime import datetime
from functools import partial
import io
from fastapi.responses import StreamingResponse
import httpx
import uuid
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright
from sqlalchemy.ext.asyncio import AsyncSession

from ...schemas.role_mapping import RoleMappingResponse

from ...schemas.cbp_plan import CBPPlanSaveRequest, CBPPlanSaveResponse, CBPPlanUpdateRequest
from ...models.user import User

from ...crud.cbp_plan import crud_cbp_plan
from ...crud.course_recommendation import crud_recommended_course
from ...crud.user_added_course import crud_user_added_course
from ...crud.role_mapping import crud_role_mapping

from ...api.dependencies import get_current_active_user
from ...core.database import get_db_session
from ...core.configs import settings
from ...core.logger import logger

from ...utils.common import convert_for_json

router = APIRouter(tags=["CBP Plans"])

# CBP Plans APIs
async def search_courses(identifiers: List[str]) -> List[Dict[str, Any]]:
    if not identifiers:
        return []

    payload = {
        "request": {
            "filters": {
                "primaryCategory": ["Course"],
                "status": ["Live"],
                "courseCategory": ["Course"],
                "identifier": identifiers
            },
            "fields": [
                "name", "identifier", "description", "keywords",
                "organisation", "competencies_v6", "language", "duration"
            ],
            "sortBy": {"createdOn": "Desc"},
            "limit": 100
        }
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.KB_BASE_URL}/api/content/v1/search",
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("content", [])

@router.post("/cbp-plan/save", response_model=CBPPlanSaveResponse)
async def save_cbp_plan(
    request: CBPPlanSaveRequest,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Save CBP (Competency-Based Program) plan with selected courses
    
    This endpoint:
    1. Fetches latest recommendation for the role mapping
    2. Extracts selected courses from filtered_courses based on course_identifiers
    3. Creates CBP plan with selected course details
    5. Stores in the table
    
    Args:
        request: CBP plan save request with role_mapping_id and course_identifiers
        
    Returns:
        CBP plan save confirmation with complete details
    """
    try:
        logger.info(f"Saving CBP plan for role mapping: {request.role_mapping_id} with {len(request.course_identifiers)} courses")
        
        # Validate role mapping exists
        role_mapping = await crud_role_mapping.get_by_id_and_user(db, request.role_mapping_id, current_user.user_id)
        
        if not role_mapping:
            logger.warning(f"Role mapping with ID {request.role_mapping_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found"
            )
        
        # Validate course identifiers are provided
        if not request.course_identifiers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one course identifier must be provided"
            )
        
        # Fetch latest recommendation for this role mapping
        latest_recommendation = await crud_recommended_course.get_by_role_mapping_id(db, request.role_mapping_id, current_user.user_id)
        
        if not latest_recommendation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No course recommendations found for this role mapping. Please generate recommendations first."
            )
        
        # Extract selected courses from filtered_courses
        filtered_courses = latest_recommendation.filtered_courses or []
        selected_courses = []
        fetched_courses = []

        filtered_lookup = {str(course.get("identifier")): course for course in filtered_courses if isinstance(course, dict)}

        for course_id in request.course_identifiers:
            course_str = str(course_id)
            if course_str in filtered_lookup:
                selected_courses.append(filtered_lookup[course_str])
                fetched_courses.append(course_str)
            else:
                logger.warning(f"Course ID {course_id} not found in filtered recommendations")
        
        # if not selected_courses:
        #     raise HTTPException(
        #         status_code=status.HTTP_400_BAD_REQUEST,
        #         detail="None of the provided course identifiers were found in the latest recommendations"
        #     )
        
        remaining_course = [c for c in request.course_identifiers if str(c) not in fetched_courses]
        
        courses_data = []
        # Use an async HTTP client to make the request
        if remaining_course:
            courses_data = await search_courses(remaining_course)
        
        remaining_course_uuid = [c for c in remaining_course if not str(c).startswith("do_")]
        user_courses = []
        if remaining_course_uuid:
            user_courses = await crud_user_added_course.get_user_added_courses_by_identifiers(
                db, 
                current_user.user_id, 
                request.role_mapping_id,
                remaining_course_uuid
            )

        # Convert ORM objects to dicts and UUIDs to strings for JSON
        user_courses_list = [
            {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in course.__dict__.items() if not k.startswith("_")}
            for course in user_courses
        ]

        selected_courses = convert_for_json(selected_courses)
        courses_data = convert_for_json(courses_data)
        user_courses_list = convert_for_json(user_courses_list)

        final_selected_courses = selected_courses + courses_data + user_courses_list

        # Create CBP plan record
        cbp_plan = await crud_cbp_plan.create(
            db,
            user_id=current_user.user_id,
            role_mapping_id=request.role_mapping_id,
            recommended_course_id=request.recommended_course_id,
            selected_courses=final_selected_courses
        )

        logger.info(f"CBP plan saved successfully with ID: {cbp_plan.id}, containing {len(selected_courses)} courses")
        return cbp_plan
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving CBP plan: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save CBP plan: {str(e)}"
        )

@router.get("/cbp-plan", response_model=CBPPlanSaveResponse)
async def get_cbp_plan(
    role_mapping_id: str = Query(..., description="Role Mapping ID to fetch CBP Plan"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Get CBP plan details by Role Mapping ID"""
    logger.info(f"Fetching CBP plan details for role mapping: {role_mapping_id}")
    try:
        
        existing_cbp_plan = await crud_cbp_plan.get_by_role_mapping(db, role_mapping_id, current_user.user_id)
        if not existing_cbp_plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No CBP Plan Details found for this role mapping. Please create a CBP Plan first."
            )
        logger.info(f"Successfully CBP Plan Details")
        return existing_cbp_plan
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting CBP plan: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get CBP plan")

@router.put("/cbp-plan/{cbp_plan_id}", response_model=CBPPlanSaveResponse)
async def update_cbp_plan(
    cbp_plan_id: str,
    request: CBPPlanUpdateRequest,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Update CBP plan details including courses, status, and metadata
    
    This endpoint allows updating:
    1. Selected courses (will re-extract from latest recommendations)
    
    Args:
        cbp_plan_id: CBP plan identifier
        request: Update request with fields to modify
        
    Returns:
        Updated CBP plan confirmation
    """
    try:
        logger.info(f"Updating CBP plan: {cbp_plan_id}")
        
        # Get existing CBP plan
        cbp_plan = await crud_cbp_plan.get_by_id(db,cbp_plan_id, current_user.user_id)
        if not cbp_plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="CBP plan not found"
            )
       
        # Update courses if new course identifiers provided
        if not request.course_identifiers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one course identifier must be provided"
            )
            
        # Fetch latest recommendation for this role mapping
        latest_recommendation = await crud_recommended_course.get_by_role_mapping_id(db, cbp_plan.role_mapping_id, current_user.user_id)
        
        if not latest_recommendation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No course recommendations found for this role mapping. Please generate recommendations first."
            )
        
        # Extract selected courses from filtered_courses
        filtered_courses = latest_recommendation.filtered_courses or []
        selected_courses = []
        fetched_courses = []

        filtered_lookup = {str(course.get("identifier")): course for course in filtered_courses if isinstance(course, dict)}

        for course_id in request.course_identifiers:
            course_str = str(course_id)
            if course_str in filtered_lookup:
                selected_courses.append(filtered_lookup[course_str])
                fetched_courses.append(course_str)
            else:
                logger.warning(f"Course ID {course_id} not found in filtered recommendations")
        
        # if not selected_courses:
        #     raise HTTPException(
        #         status_code=status.HTTP_400_BAD_REQUEST,
        #         detail="None of the provided course identifiers were found in the latest recommendations"
        #     )
        
        remaining_course = [c for c in request.course_identifiers if str(c) not in fetched_courses]
        
        courses_data = []
        # Use an async HTTP client to make the request
        if remaining_course:
            courses_data = await search_courses(remaining_course)
        
        remaining_course_uuid = [c for c in remaining_course if not str(c).startswith("do_")]
        user_courses = []
        if remaining_course_uuid:
            user_courses = await crud_user_added_course.get_user_added_courses_by_identifiers(
                db, 
                current_user.user_id, 
                cbp_plan.role_mapping_id,
                remaining_course_uuid
            )

        # Convert ORM objects to dicts and UUIDs to strings for JSON
        user_courses_list = [
            {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in course.__dict__.items() if not k.startswith("_")}
            for course in user_courses
        ]

        selected_courses = convert_for_json(selected_courses)
        courses_data = convert_for_json(courses_data)
        user_courses_list = convert_for_json(user_courses_list)

        final_selected_courses = selected_courses + courses_data + user_courses_list
        
        # Update courses and related fields
        update_records = {'selected_courses': final_selected_courses}
        cbp_plan = await crud_cbp_plan.update(db, cbp_plan_id, update_records)
        logger.info(f"CBP plan {cbp_plan_id} updated successfully.")
        return cbp_plan
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating CBP plan: {str(e)}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update CBP plan: {str(e)}"
        )

class DesignationData:
    """Formatted designation data for template rendering"""
    def __init__(self, cbp_record: RoleMappingResponse):
        self.designation = cbp_record.designation_name
        self.wing = cbp_record.wing_division_section
        self.roles_responsibilities = cbp_record.role_responsibilities
        self.activities = cbp_record.activities
        
        # Group competencies by type
        self.behavioral_competencies = []
        self.functional_competencies = []
        self.domain_competencies = []

        for comp in cbp_record.competencies:
            comp_str = f"{comp['theme']} - {comp['sub_theme']}"
            comp_type = comp['type'].lower()
           
            if "behavioral" in comp_type:
                self.behavioral_competencies.append(comp_str)
            elif "functional" in comp_type:
                self.functional_competencies.append(comp_str)
            elif "domain" in comp_type:
                self.domain_competencies.append(comp_str)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "designation": self.designation,
            "wing": self.wing,
            "rolesResponsibilities": self.roles_responsibilities,
            "activities": self.activities,
            "behavioralCompetencies": self.behavioral_competencies,
            "functionalCompetencies": self.functional_competencies,
            "domainCompetencies": self.domain_competencies
        }


def _render_template_sync(cbp_records: List[RoleMappingResponse], center_department_name: str) -> str:
    """
    Generate HTML by binding CBP data to Jinja2 template
    """
    # Prepare designation data
    designation_list = [DesignationData(record) for record in cbp_records]
    designation_data = [d.to_dict() for d in designation_list]

    total_behavioral = sum(len(d["behavioralCompetencies"]) for d in designation_data)
    total_functional = sum(len(d["functionalCompetencies"]) for d in designation_data)
    total_domain = sum(len(d["domainCompetencies"]) for d in designation_data)
    total_competencies = total_behavioral + total_functional + total_domain
    
    stats = {
        "center_department_name": center_department_name,
        "total_behavioral": total_behavioral,
        "total_functional": total_functional,
        "total_domain": total_domain,
        "total_competencies": total_competencies,
        "behavioral_percentage": round((total_behavioral / total_competencies * 100) if total_competencies > 0 else 0, 1),
        "functional_percentage": round((total_functional / total_competencies * 100) if total_competencies > 0 else 0, 1),
        "domain_percentage": round((total_domain / total_competencies * 100) if total_competencies > 0 else 0, 1),
    }

    # Render template
    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("cbp_template.html")
    html_output = template.render(
        designations=designation_data,
        stats=stats,
        current_year=datetime.now().year
    )

    return html_output

async def generate_html_content(cbp_records: List[RoleMappingResponse], center_department_name: str) -> str:
    """
    Async wrapper that offloads rendering to a thread.
    REMOVED: File writing to "report.html" to improve I/O speed.
    """
    loop = asyncio.get_running_loop()
    # partial is used to pass arguments to the function in the executor
    return await loop.run_in_executor(None, partial(_render_template_sync, cbp_records, center_department_name))
   
async def convert_html_to_pdf(html_content: str) -> bytes:
    """
    Convert HTML to PDF using Playwright (Chromium headless)

    Args:
        html_content: HTML string to convert
    Returns:
        PDF bytes
    """
    browser = None

    try:
        async with async_playwright() as p:
            # Launch options can be tuned for performance
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            
            # Open a new page
            page = await browser.new_page()
            
            # OPTIMIZATION: Use set_content instead of writing to a temp file
            # This keeps everything in memory and avoids disk I/O
            await page.set_content(html_content, wait_until="networkidle")

            pdf_bytes = await page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "20px", "bottom": "20px", "left": "20px", "right": "20px"}
            )

            await browser.close()
            return pdf_bytes
    except Exception as e:
        logger.error(f"Error in Playwright PDF generation: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Error generating PDF with Playwright"
        )
    finally:
        # Ensure cleanup
        if browser:
            try:
                logger.info("Closing Playwright browser...")
                await browser.close()
            except Exception as e:
                logger.exception("Error closing browser")
        


@router.get("/cbp-plan/download")
async def download_cbp_plan(
    state_center_id: str,
    department_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Generate and download CBP report as PDF

    Returns a downloadable PDF file
    """
    logger.info(f"Fetching CBP plan details for pdf: {state_center_id}")
    try:  
        # Check if role mapping already exists
        role_mapping = await crud_role_mapping.get_all_completed_mapping(db,state_center_id, current_user.user_id,  department_id)
        
        if not role_mapping:
            logger.info(f"State/center with ID {state_center_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"State/center with ID {state_center_id} not found"
            )
        
        center_department_name = None
        center_department_name = role_mapping[0].state_center_name
        if department_id:
            center_department_name = role_mapping[0].department_name
        
        # Generate HTML with data binding
        html_content = await generate_html_content(role_mapping, center_department_name)
        
        # Convert to PDF
        pdf_bytes = await convert_html_to_pdf(html_content)
        
        # Create streaming response
        pdf_stream = io.BytesIO(pdf_bytes)
        
        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"CBP_Report_{state_center_id}_{timestamp}.pdf"
        logger.info(f"Generated PDF report for cbp plan : {filename}")
        return StreamingResponse(
            pdf_stream,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Type": "application/pdf"
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error while downloading CBP plan: {str(e)}")
        raise HTTPException(status_code=500, detail="Error generating PDF report for cbp plan")