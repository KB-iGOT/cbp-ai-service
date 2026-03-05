import asyncio
from datetime import datetime
from enum import Enum
from functools import partial
import io
import time
import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
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
from ...services.translation_service import translation_service

router = APIRouter(prefix="/reports", tags=["Reports"])


class ReportType(str, Enum):
    """Report template types"""
    COURSE_RECOMMENDATION = "view_course_recommendation_template.html"
    CBP = "cbp_template.html"
    ACBP = "acbp_template.html"


class ReportService:
    """Centralized service for report generation"""
    
    TRANSLATE_KEYS = [
        "role_responsibilities",
        "activities",
        "rationale",
        "designation_name",
        "wing_division_section"
    ]
    
    def __init__(self):
        self.template_env = Environment(loader=FileSystemLoader("templates"))
    
    def _calculate_competency_stats(self, designation_data: List[dict]) -> dict:
        """Calculate unique competency statistics across all designations"""
        unique_behavioral = set()
        unique_functional = set()
        unique_domain = set()
        for d in designation_data:
            unique_behavioral.update(comp.strip().lower() for comp in d["behavioralCompetencies"])
            unique_functional.update(comp.strip().lower() for comp in d["functionalCompetencies"])
            unique_domain.update(comp.strip().lower() for comp in d["domainCompetencies"])
        total_behavioral = len(unique_behavioral)
        total_functional = len(unique_functional)
        total_domain = len(unique_domain)
        total_competencies = total_behavioral + total_functional + total_domain
        
        return {
            "total_behavioral": total_behavioral,
            "total_functional": total_functional,
            "total_domain": total_domain,
            "total_competencies": total_competencies,
            "behavioral_percentage": round(
                (total_behavioral / total_competencies * 100) if total_competencies > 0 else 0, 1
            ),
            "functional_percentage": round(
                (total_functional / total_competencies * 100) if total_competencies > 0 else 0, 1
            ),
            "domain_percentage": round(
                (total_domain / total_competencies * 100) if total_competencies > 0 else 0, 1
            ),
        }
    
    def render_course_recommendation_template(
        self, 
        course_records: CBPPlanSaveResponse, 
        center_department_name: str, 
        designation_name: str
    ) -> str:
        """Generate HTML for course recommendation report"""
        courses = [CourseCardData(course).to_dict() for course in course_records.selected_courses]
        
        stats = {
            "center_department_name": center_department_name,
            "total_courses": len(courses),
            "igot_courses": sum(1 for c in courses if not c.get("is_public")),
            "public_courses": sum(1 for c in courses if c.get("is_public")),
        }
        
        template = self.template_env.get_template(ReportType.COURSE_RECOMMENDATION.value)
        return template.render(
            courses=courses,
            stats=stats,
            designation_name=designation_name,
            current_year=datetime.now().year
        )
    
    def render_cbp_template(
        self, 
        cbp_records: List[RoleMappingResponse], 
        center_department_name: str,
        template_type: ReportType = ReportType.CBP
    ) -> str:
        """Generate HTML for CBP/ACBP report"""
        designation_data = [DesignationData(record).to_dict() for record in cbp_records]
        
        stats = self._calculate_competency_stats(designation_data)
        stats["center_department_name"] = center_department_name
        
        template = self.template_env.get_template(template_type.value)
        return template.render(
            designations=designation_data,
            stats=stats,
            current_year=datetime.now().year
        )
    
    async def translate_role_mapping(
        self,
        role_mapping: dict,
        target_language: str
    ) -> dict:
        """Translate a single role mapping object"""
        if target_language == "en" or not translation_service.is_enabled():
            return role_mapping
        
        translated = role_mapping.copy()
        
        # Prepare batches for parallel translation
        batch_1 = []  # Single fields: designation_name, wing_division_section, rationale
        batch_2 = []  # role_responsibilities
        batch_3 = []  # activities
        
        # Collect texts for batch 1
        if "designation_name" in role_mapping:
            batch_1.append(role_mapping["designation_name"])
        if "wing_division_section" in role_mapping:
            batch_1.append(role_mapping["wing_division_section"])
        if "rationale" in role_mapping:
            batch_1.append(role_mapping["rationale"])
        
        # Collect texts for batch 2
        if "role_responsibilities" in role_mapping and isinstance(role_mapping["role_responsibilities"], list):
            batch_2 = role_mapping["role_responsibilities"]
        
        # Collect texts for batch 3
        if "activities" in role_mapping and isinstance(role_mapping["activities"], list):
            batch_3 = role_mapping["activities"]
        
        # Translate all batches in parallel
        results = await asyncio.gather(
            translation_service.translate_batch(batch_1, source_language="en", target_language=target_language) if batch_1 else asyncio.sleep(0, result=[]),
            translation_service.translate_batch(batch_2, source_language="en", target_language=target_language) if batch_2 else asyncio.sleep(0, result=[]),
            translation_service.translate_batch(batch_3, source_language="en", target_language=target_language) if batch_3 else asyncio.sleep(0, result=[]),
            return_exceptions=True
        )
        
        # Map results back
        batch_1_results = results[0] if not isinstance(results[0], Exception) else batch_1
        batch_2_results = results[1] if not isinstance(results[1], Exception) else batch_2
        batch_3_results = results[2] if not isinstance(results[2], Exception) else batch_3
        
        # Apply translations
        idx = 0
        if "designation_name" in role_mapping and batch_1_results:
            translated["designation_name"] = batch_1_results[idx]
            idx += 1
        if "wing_division_section" in role_mapping and batch_1_results and idx < len(batch_1_results):
            translated["wing_division_section"] = batch_1_results[idx]
            idx += 1
        if "rationale" in role_mapping and batch_1_results and idx < len(batch_1_results):
            translated["rationale"] = batch_1_results[idx]
        
        if batch_2_results:
            translated["role_responsibilities"] = batch_2_results
        
        if batch_3_results:
            translated["activities"] = batch_3_results
        
        return translated
    
    async def translate_role_mappings(
        self,
        role_mappings: List[dict],
        target_language: str
    ) -> List[dict]:
        """Translate multiple role mapping objects in parallel"""
        if target_language == "en" or not translation_service.is_enabled():
            return role_mappings
        
        logger.info(f"Translating {len(role_mappings)} role mappings to {target_language}")
        start_time = time.time()
        # Translate all role mappings in parallel
        translated_mappings = await asyncio.gather(
            *[self.translate_role_mapping(rm, target_language) for rm in role_mappings],
            return_exceptions=True
        )
        end_time = time.time()
        print(f"Translation took {end_time - start_time} seconds")
        
        # Handle any exceptions
        result = []
        for i, translated in enumerate(translated_mappings):
            if isinstance(translated, Exception):
                logger.error(f"Error translating role mapping {i}: {str(translated)}")
                result.append(role_mappings[i])  # Use original on error
            else:
                result.append(translated)
        
        logger.info(f"Successfully translated {len(result)} role mappings")
        return result


# Initialize service
report_service = ReportService()


async def render_template_async(render_func, *args, **kwargs) -> str:
    """Generic async wrapper for template rendering"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, 
        partial(render_func, *args, **kwargs)
    )


async def generate_pdf_response(
    html_content: str,
    filename_prefix: str,
    identifier: str
) -> StreamingResponse:
    """Generate PDF and create streaming response"""
    pdf_bytes = await convert_html_to_pdf(html_content)
    pdf_stream = io.BytesIO(pdf_bytes)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{filename_prefix}_{identifier}_{timestamp}.pdf"
    
    logger.info(f"Generated PDF report: {filename}")
    
    return StreamingResponse(
        pdf_stream,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": "application/pdf"
        }
    )


def get_center_department_name(role_mapping, department_id: Optional[str] = None) -> str:
    """Extract center/department name from role mapping"""
    if department_id and role_mapping.department_name:
        return role_mapping.department_name
    return role_mapping.state_center_name


@router.get("/course-recommendations/download")
async def download_course_recommendation_report(
    role_mapping_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Download a Course Recommendation report as a PDF"""
    logger.info(f"Fetching course recommendation details for pdf: {role_mapping_id}")
    
    try:
        course_recommendations = await crud_cbp_plan.get_by_role_mapping(
            db, role_mapping_id, current_user.user_id
        )
        
        if not course_recommendations:
            logger.info(f"Role mapping with ID {role_mapping_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Role Mapping with ID {role_mapping_id} not found"
            )
        
        role_mapping = await crud_role_mapping.get_by_id(role_mapping_id)
        center_department_name = get_center_department_name(role_mapping)
        
        # Generate HTML
        html_content = await render_template_async(
            report_service.render_course_recommendation_template,
            course_recommendations,
            center_department_name,
            role_mapping.designation_name
        )
        
        # Generate and return PDF
        return await generate_pdf_response(
            html_content,
            "Course_Report",
            str(role_mapping_id)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error while downloading course recommendation report")
        raise HTTPException(
            status_code=500,
            detail="Error generating PDF report for course recommendation"
        )


async def download_plan_report(
    state_center_id: str,
    report_type: ReportType,
    report_name: str,
    language: str = "en",
    department_id: Optional[str] = None,
    load_cbp_plans: bool = False,
    db: AsyncSession = None,
    current_user: User = None
):
    """Generic handler for CBP/ACBP plan downloads with language support"""
    logger.info(f"Fetching {report_name} plan details for pdf: {state_center_id}, language: {language}")
    
    # Validate language
    if language not in translation_service.supported_languages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported language: {language}. Supported languages: {', '.join(translation_service.supported_languages)}"
        )
    
    try:
        role_mapping = await crud_role_mapping.get_all_completed_mapping(
            db, state_center_id, current_user.user_id, department_id, load_cbp_plans=load_cbp_plans
        )
        
        if not role_mapping:
            logger.info(f"State/center with ID {state_center_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"State/center with ID {state_center_id} not found"
            )
        
        # Translate if language is not English
        if language != "en" and translation_service.is_enabled():
            logger.info(f"Translating role mappings to {language}")
            # Convert to dict for translation
            role_mapping_dicts = [rm.model_dump() if hasattr(rm, 'model_dump') else rm.__dict__ for rm in role_mapping]
            translated_dicts = await report_service.translate_role_mappings(role_mapping_dicts, language)
            
            # Convert back to response objects
            role_mapping = [RoleMappingResponse(**rm_dict) for rm_dict in translated_dicts]
        
        center_department_name = get_center_department_name(role_mapping[0], department_id)
        
        # Generate HTML
        html_content = await render_template_async(
            report_service.render_cbp_template,
            role_mapping,
            center_department_name,
            report_type
        )
        
        # Generate and return PDF
        return await generate_pdf_response(
            html_content,
            f"{report_name}_Report",
            state_center_id
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error while downloading {report_name} plan: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error generating PDF report for {report_name.lower()} plan"
        )


@router.get("/cbp-plan/download")
async def download_cbp_plan(
    state_center_id: str,
    department_id: Optional[str] = None,
    language: str = Query("en", description="Language code for the report (e.g., en, hi, te, kn, mr, ta, gu, ml, or, pa, bn, as)"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Generate and download CBP report as PDF with language support"""
    return await download_plan_report(
        state_center_id=state_center_id,
        report_type=ReportType.CBP,
        report_name="CBP",
        language=language,
        department_id=department_id,
        load_cbp_plans=False,
        db=db,
        current_user=current_user
    )


@router.get("/acbp-plan/download")
async def download_acbp_plan(
    state_center_id: str,
    department_id: Optional[str] = None,
    language: str = Query("en", description="Language code for the report (e.g., en, hi, te, kn, mr, ta, gu, ml, or, pa, bn, as)"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Generate and download ACBP report as PDF with language support"""
    return await download_plan_report(
        state_center_id=state_center_id,
        report_type=ReportType.ACBP,
        report_name="ACBP",
        language=language,
        department_id=department_id,
        load_cbp_plans=True,
        db=db,
        current_user=current_user
    )