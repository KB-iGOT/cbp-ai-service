import asyncio
from datetime import datetime
from functools import partial
import io
import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import  StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from jinja2 import Environment, FileSystemLoader

from ...utils.common import convert_html_to_pdf

from ...schemas.cbp_plan import CBPPlanSaveResponse
from ...models.user import User
from ...schemas.report import CourseCardData, DesignationData
from ...schemas.role_mapping import RoleMappingResponse

from ...core.database import get_db_session
from ...core.logger import logger

from ...crud.role_mapping import crud_role_mapping
from ...crud.cbp_plan import crud_cbp_plan

from ...api.dependencies import get_current_active_user

router = APIRouter(prefix="/reports",tags=["Reports"])

def _render_template_sync(course_records: CBPPlanSaveResponse, center_department_name: str, designation_name: str) -> str:
    """
    Generate HTML by binding Course Recommendation data to Jinja2 template
    """
    selected_courses = course_records.selected_courses

    courses = [CourseCardData(course).to_dict() for course in selected_courses]

    stats = {
        "center_department_name": center_department_name,
        "total_courses": len(courses),
        "igot_courses": sum(1 for c in courses if not c.get("is_public")),
        "public_courses": sum(1 for c in courses if c.get("is_public")),
    }

    # Render template
    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("view_course_recommendation_template.html")
    html_output = template.render(
        courses=courses,
        stats=stats,
        designation_name=designation_name,
        current_year=datetime.now().year
    )
    return html_output

async def generate_html_content(course_records: CBPPlanSaveResponse, center_department_name: str, designation_name: str) -> str:
    """
    Async wrapper that offloads rendering to a thread.
    REMOVED: File writing to "report.html" to improve I/O speed.
    """
    loop = asyncio.get_running_loop()
    # partial is used to pass arguments to the function in the executor
    return await loop.run_in_executor(None, partial(_render_template_sync, course_records, center_department_name, designation_name))
   
@router.get("/course-recommendations/download")
async def download_course_recommendation_report(
    role_mapping_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Download a Course Recommendation report as a PDF based on the specified role mapping.

    Returns a downloadable PDF file
    """
    logger.info(f"Fetching course recommendation details for pdf: {role_mapping_id}")
    try:  
        course_recommendations = await crud_cbp_plan.get_by_role_mapping(db,role_mapping_id, current_user.user_id)
        
        if not course_recommendations:
            logger.info(f"Role mapping with ID {role_mapping_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Role Mapping with ID {role_mapping_id} not found"
            )
        role_mapping = await crud_role_mapping.get_by_id(role_mapping_id)
        designation_name = role_mapping.designation_name
        center_department_name = role_mapping.state_center_name
        if role_mapping.department_name:
            center_department_name = role_mapping.department_name
        
        # Generate HTML with data binding
        html_content = await generate_html_content(course_recommendations, center_department_name, designation_name)
        
        # Convert to PDF
        pdf_bytes = await convert_html_to_pdf(html_content)
        
        # Create streaming response
        pdf_stream = io.BytesIO(pdf_bytes)
        
        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"Course_Report_{role_mapping_id}_{timestamp}.pdf"
        logger.info(f"Generated PDF report for course recommedation : {filename}")
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
        logger.exception("Error while downloading course recommendation report")
        raise HTTPException(status_code=500, detail="Error generating PDF report for course recommendation")
    
def _render_template_sync_cbp(cbp_records: List[RoleMappingResponse], center_department_name: str) -> str:
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

async def generate_html_content_cbp(cbp_records: List[RoleMappingResponse], center_department_name: str) -> str:
    """
    Async wrapper that offloads rendering to a thread.
    REMOVED: File writing to "report.html" to improve I/O speed.
    """
    loop = asyncio.get_running_loop()
    # partial is used to pass arguments to the function in the executor
    return await loop.run_in_executor(None, partial(_render_template_sync_cbp, cbp_records, center_department_name))
      
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
        html_content = await generate_html_content_cbp(role_mapping, center_department_name)
        
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
    

def _render_template_sync_acbp(cbp_records: List[RoleMappingResponse], center_department_name: str) -> str:
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
    template = env.get_template("acbp_template.html")
    html_output = template.render(
        designations=designation_data,
        stats=stats,
        current_year=datetime.now().year
    )
    
    return html_output

async def generate_html_content_acbp(cbp_records: List[RoleMappingResponse], center_department_name: str) -> str:
    """
    Async wrapper that offloads rendering to a thread.
    REMOVED: File writing to "report.html" to improve I/O speed.
    """
    loop = asyncio.get_running_loop()
    # partial is used to pass arguments to the function in the executor
    return await loop.run_in_executor(None, partial(_render_template_sync_acbp, cbp_records, center_department_name))
      
@router.get("/acbp-plan/download")
async def download_acbp_plan(
    state_center_id: str,
    department_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Generate and download ACBP report as PDF

    Returns a downloadable PDF file
    """
    logger.info(f"Fetching ACBP plan details for pdf: {state_center_id}")
    try:  
        # Check if role mapping already exists
        role_mapping = await crud_role_mapping.get_all_completed_mapping(db,state_center_id, current_user.user_id,  department_id, load_cbp_plans=True)
        
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
        html_content = await generate_html_content_acbp(role_mapping, center_department_name)
        
        # Convert to PDF
        pdf_bytes = await convert_html_to_pdf(html_content)
        
        # Create streaming response
        pdf_stream = io.BytesIO(pdf_bytes)
        
        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ACBP_Report_{state_center_id}_{timestamp}.pdf"
        logger.info(f"Generated PDF report for acbp plan : {filename}")
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
        logger.error(f"Error while downloading ACBP plan: {str(e)}")
        raise HTTPException(status_code=500, detail="Error generating PDF report for acbp plan")