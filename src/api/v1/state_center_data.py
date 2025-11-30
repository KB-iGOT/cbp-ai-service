import asyncio
from typing import Optional
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.state_center_data import StateCenterData
from ...schemas.state_center_data import FileUploadResponse, StateCenterDataResponse

from ...core.configs import settings
from ...models.user import User
from ...crud.state_center_data import crud_state_center_data

from ...api.dependencies import require_role
from ...core.database import get_db_session
from ...services.pdf_service import pdf_service
from ...core.logger import logger


router = APIRouter(tags=["State Centers Data"])

async def process_documents_background(record_id: uuid.UUID, acbp_bytes: Optional[bytes], work_bytes: Optional[bytes]):
    try:
        record = await crud_state_center_data.get_by_id(record_id)
        if not record:
            return
        
        tasks = []
        if acbp_bytes:
            tasks.append(pdf_service.process_pdf_and_generate_summary(acbp_bytes, "acbp_plan"))
        if work_bytes:
            tasks.append(pdf_service.process_pdf_and_generate_summary(work_bytes, "work_allocation"))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        idx, success_count, fail_count = 0, 0, 0
        errors = []
        update_record = {}
        if acbp_bytes:
            if isinstance(results[idx], Exception):
                errors.append(f"ACBP Plan: {results[idx]}")
                update_record['acbp_plan_summary'] = None
                fail_count += 1
            else:
                update_record['acbp_plan_summary'] = results[idx]
                success_count += 1
            idx += 1

        if work_bytes:
            if isinstance(results[idx], Exception):
                errors.append(f"Work Allocation: {results[idx]}")
                update_record['work_allocation_order_summary'] = None
                fail_count += 1
            else:
                update_record['work_allocation_order_summary'] = results[idx]
                success_count += 1

        if success_count > 0 and fail_count == 0:
            update_record['status'] = "completed"
            update_record['error_message'] = None
        elif success_count > 0 and fail_count > 0:
            update_record['status'] = "completed_with_errors"
            update_record['error_message'] = " | ".join(errors)
        else:
            update_record['status'] = "failed"
            update_record['error_message'] = " | ".join(errors)

        await crud_state_center_data.update(record_id, update_record)

    except Exception as e:
        logger.error(f"Background processing failed: {e}")
        record = await crud_state_center_data.get_by_id(record_id)
        if record:
            update_record = {
                'status' : "failed",
                'error_message': str(e)
            }
            await crud_state_center_data.update(record_id, update_record)

# State Center Data APIs
@router.post("/state-center-data/upload_documents_background", response_model=FileUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_state_center_documents_in_background(
    background_tasks: BackgroundTasks,
    state_center_id: str = Form(..., description="ID of the state/center"),
    department_id: Optional[str] = Form(None, description="Optional Department ID"),
    acbp_plan_pdf: UploadFile = File(None, description="ACBP Plan PDF file"),
    work_allocation_pdf: UploadFile = File(None, description="Work Allocation Order PDF file"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """
    Upload ACBP Plan and Work Allocation Order PDFs for a state/center.
    Summaries will be generated asynchronously in background.
    """
    try:
        logger.info(f"Received upload request for state/center ID: {state_center_id}")

        if not acbp_plan_pdf and not work_allocation_pdf:
            raise HTTPException(status_code=400, detail="At least one PDF must be provided")
        
        existing_data = await crud_state_center_data.get_by_state_center_and_department(state_center_id,  department_id)

        acbp_filename, acbp_bytes = None, None
        work_filename, work_bytes = None, None

        if acbp_plan_pdf:
            if not acbp_plan_pdf.filename.lower().endswith(".pdf"):
                raise HTTPException(status_code=400, detail="Invalid ACBP Plan PDF")
            acbp_bytes = await acbp_plan_pdf.read()
            if len(acbp_bytes) > settings.PDF_MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail="ACBP Plan PDF too large")
            acbp_filename = acbp_plan_pdf.filename

        if work_allocation_pdf:
            if not work_allocation_pdf.filename.lower().endswith(".pdf"):
                raise HTTPException(status_code=400, detail="Invalid Work Allocation PDF")
            work_bytes = await work_allocation_pdf.read()
            if len(work_bytes) > settings.PDF_MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail="Work Allocation PDF too large")
            work_filename = work_allocation_pdf.filename

        if existing_data:
            update_record = {
                'acbp_plan_filename' : acbp_filename or existing_data.acbp_plan_filename,
                'work_allocation_filename' : work_filename or existing_data.work_allocation_filename,
                'status': "processing",
                'error_message': None
            }
            inserted_data = await crud_state_center_data.update(existing_data.id, update_record)
        else:
            db_state_center_data = StateCenterData(
                id=uuid.uuid4(),
                department_id=department_id,
                state_center_id=state_center_id,
                acbp_plan_filename=acbp_filename,
                work_allocation_filename=work_filename,
                status="processing"
            )
            inserted_data = await crud_state_center_data.create(db, db_state_center_data)

        # Fire background task
        # asyncio.create_task(process_documents_background(inserted_data.id, acbp_bytes, work_bytes))
        background_tasks.add_task(
            process_documents_background,
            inserted_data.id,
            acbp_bytes,
            work_bytes
        )

        return FileUploadResponse(
            message="Upload received. Summaries are being generated in background.",
            data=inserted_data
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error preparing upload: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start processing: {e}")

@router.get("/state-center-data", response_model=StateCenterDataResponse)
async def get_state_center_data(
    state_center_id: str = Query(..., description="State/Center ID"),
    department_id: Optional[str] = Query(None, description="Optional department ID"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """Get state center data including document summaries for a specific state/center"""
    try:
        logger.info(f"Fetching state center data for state/center ID: {state_center_id}")
        
        # Get state center data
        state_center_data = await crud_state_center_data.get_by_state_center_and_department(state_center_id, department_id)

        
        if not state_center_data:
            logger.warning(f"No data found for state/center ID: {state_center_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No document data found for this state/center"
            )
        
        logger.info(f"Retrieved state center data for {state_center_id}")
        return state_center_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching state center data: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch state center data"
        )

@router.delete("/state-center-data", status_code=status.HTTP_204_NO_CONTENT)
async def delete_state_center_data(
    state_center_id: str = Query(..., description="State/Center ID"),
    type: str = Query(..., description="Type of data to clear: 'acbp_doc' or 'work_doc'"),
    department_id: Optional[str] = Query(None, description="Optional department ID"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """Delete/clear specific summary data for a state center based on type"""
    try:
        logger.info(f"Attempting to clear {type} data for state/center ID: {state_center_id}")
        
        # Validate type parameter
        if type not in ["acbp_doc", "work_doc"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Type must be either 'acbp_doc' or 'work_doc'"
            )
        
        # Find the state center data to update
        state_center_data = await crud_state_center_data.get_by_state_center_and_department(state_center_id, department_id)
        
        if not state_center_data:
            logger.warning(f"No data found for state/center ID: {state_center_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No document data found for this state/center"
            )
        
        # Clear specific summary fields based on type
        if type == "acbp_doc":
            update_record = {
                'acbp_plan_filename':  None,
                'acbp_plan_summary': None
            }
            state_center_data = await crud_state_center_data.update(state_center_data.id, update_record)
            logger.info(f"Clearing acbp_summary for state/center: {state_center_id}")
        elif type == "work_doc":
            update_record = {
                'work_allocation_filename':  None,
                'work_allocation_order_summary': None
            }
            state_center_data = await crud_state_center_data.update(state_center_data.id, update_record)
            logger.info(f"Clearing work allocation summary for state/center: {state_center_id}")
        
        # Check if both summaries are now empty/null
        if (state_center_data.acbp_plan_summary is None or state_center_data.acbp_plan_summary == "") and \
           (state_center_data.work_allocation_order_summary is None or state_center_data.work_allocation_order_summary == ""):
            # Delete the entire record if both summaries are empty
            await crud_state_center_data.delete(db, state_center_data)
            logger.info(f"Both summaries are empty, deleting entire record for {state_center_id}")
            action = "deleted entire record"
        else:
            # Just update the record if at least one summary still has content
            logger.info(f"At least one summary still has content, updating record for {state_center_id}")
            action = f"cleared {type} data"
        
        logger.info(f"Successfully {action} for {state_center_id}")
        
        logger.info(f"Successfully cleared {type} data for {state_center_id}")
        return
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error clearing {type} data: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clear {type} data"
        )
