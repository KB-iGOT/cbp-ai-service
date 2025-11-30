import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_active_user
from ...core.database import get_db_session
from ...models.document import Document
from ...models.meta_summary import MetaSummary
from ...schemas.meta_summary import MetaSummaryCreateRequest, MetaSummaryDeleteResponse, MetaSummaryListResponse, MetaSummaryResponse
from ...crud.document import crud_document
from ...crud.meta_summary import crud_meta_summary
from .document_routes import get_genai_client, _run_document_summary
from ...prompts.prompts import META_SUMMARY_PROMPT

from google.genai import types

from ...core.logger import logger

router = APIRouter(prefix="/meta-summaries", tags=["Meta Summaries"]) 

# Background task

async def _run_meta_summary(request_id: uuid.UUID):
    try:
        logger.info(f"Meta summary process started for {request_id}")
        batch: MetaSummary = await crud_meta_summary.get_by_request_id(request_id)
        if not batch:
            logger.warning(f"MetaSummary {request_id} missing")
            return
        if batch.status in ("IN_PROGRESS", "COMPLETED"):
            return
        
        await crud_meta_summary.update(request_id, {'status': 'IN_PROGRESS'})

        # Ensure all file summaries exist
        summaries_text_parts: List[str] = []
        for fid in batch.file_ids:
            doc: Document = await crud_document.get_by_id(fid)
            if not doc:
                await crud_meta_summary.update(request_id, {
                    'status': 'FAILED',
                    'error_message': 'f"Document {fid} not found"'
                })
                return
            if doc.summary_status == "FAILED":
                _run_document_summary(doc.file_id)
            if doc.summary_status == "NOT_STARTED":
                _run_document_summary(doc.file_id)
            if doc.summary_status != "COMPLETED":
                await crud_meta_summary.update(request_id, {
                    'status': 'FAILED',
                    'error_message': f"Document {doc.id} summary not completed (status={doc.summary_status})"
                })
                return
            summaries_text_parts.append(f"--- {doc.filename} ({doc.file_id}) ---\n{doc.summary_text}\n")

        joined = "\n".join(summaries_text_parts)
        prompt_text = META_SUMMARY_PROMPT.format(payload=joined)

        client = get_genai_client()
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=prompt_text)]
            )
        ]
        generate_content_config = types.GenerateContentConfig(
            temperature=0.6,
            top_p=0.95,
            max_output_tokens=8192,
            safety_settings=[
                types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
            ],
        )
        try:
            response = client.models.generate_content(
                model="gemini-2.5-pro",
                contents=contents,
                config=generate_content_config
            )
            meta_text = response.text
            if not meta_text:
                raise RuntimeError("Empty meta summary returned")

            await crud_meta_summary.update(request_id, {
                'status': 'COMPLETED',
                'summary_text': meta_text,
                'error_message': None
            })
            logger.info(f"Meta summary process completed for {request_id}")
        except Exception as e:
            logger.exception("Meta summary generation failed")
            await crud_meta_summary.update(request_id, {
                'status': 'FAILED',
                'summary_text': None,
                'error_message': str(e)
            })
    except Exception as e:
        logger.exception("Meta summary generation failed")

@router.post("", response_model=MetaSummaryResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_meta_summary(
    req: MetaSummaryCreateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_active_user)
):
    # Validate file IDs and remove duplicates preserving order
    try:
        unique_ids = list(dict.fromkeys(req.file_ids))

        if not unique_ids:
            raise HTTPException(status_code=400, detail="No valid file ids provided")

        # Check existence quickly
        docs = await crud_document.get_by_identifiers(db, unique_ids)
        if len(docs) != len(unique_ids):
            existing_ids = {d.file_id for d in docs}
            missing = [str(fid) for fid in unique_ids if fid not in existing_ids]
            raise HTTPException(status_code=400, detail=f"Some file ids not found: {missing}")

        state_center_ids = {doc.state_center_id for doc in docs}
        if len(state_center_ids) > 1:
            raise HTTPException(
                status_code=400,
                detail="All documents must belong to the same state/center"
            )
        state_center_id = state_center_ids.pop()
        
        department_ids = {doc.department_id for doc in docs}
        if len(department_ids) > 1:
            raise HTTPException(
                status_code=400,
                detail=f"Not all documents belong to the same department"
            )
        department_id = department_ids.pop()

        batch = MetaSummary(
            state_center_id=state_center_id,
            department_id=department_id,
            file_ids=[str(fid) for fid in unique_ids], 
            status="PENDING")

        batch = await crud_meta_summary.create(db, batch)

        background_tasks.add_task(_run_meta_summary, batch.request_id)

        return MetaSummaryResponse(
            request_id=batch.request_id,
            state_center_id=state_center_id,
            department_id=department_id,
            status=batch.status,
            file_ids=[uuid.UUID(x) for x in batch.file_ids],
            summary_text=None,
            error_message=None,
            created_at=batch.created_at,
            updated_at=batch.updated_at
        )
    except Exception as e:
        logger.exception("Error while creating meta summary failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create meta summary"
        )

@router.get("", response_model=MetaSummaryListResponse)
async def list_meta_summaries(
    status: Optional[str] = Query(None, description="Filter by status (PENDING, IN_PROGRESS, COMPLETED, FAILED)"),
    state_center_id: Optional[str] = Query(None, description="Filter by state center ID"),
    department_id: Optional[str] = Query(None, description="Filter by department ID"),
    skip: int = Query(0, ge=0, description="Number of items to skip"),
    limit: int = Query(50, ge=1, le=200, description="Number of items to return"),
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_active_user)
):
    """List all meta-summary requests with filtering and pagination"""
    try:
        total, meta_summaries = await crud_meta_summary.get_meta_summaries(
            db,
            state_center_id,
            department_id,
            status,
            skip,
            limit
        )
        return MetaSummaryListResponse(items=meta_summaries, total=total)
    except Exception as e:
        logger.exception("Error while creating meta summary failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create meta summary"
        )


@router.get("/{request_id}", response_model=MetaSummaryResponse)
async def get_meta_summary(request_id: uuid.UUID, db: AsyncSession = Depends(get_db_session), current_user=Depends(get_current_active_user)):
    try:
        batch: MetaSummary = await crud_meta_summary.get_by_request_id(request_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Meta summary request not found")
        return batch
    except Exception as e:
        logger.exception("Error while fetching meta summary failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch meta summary"
        )

@router.delete("/{request_id}", response_model=MetaSummaryDeleteResponse)
async def delete_meta_summary(
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_active_user)
):
    """Delete a meta-summary by request_id"""
    batch: MetaSummary = await crud_meta_summary.get_by_request_id(request_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Meta summary request not found")
    
    await crud_meta_summary.delete_by_id(db, request_id)
    
    return MetaSummaryDeleteResponse(
        message="Meta summary deleted successfully",
        request_id=request_id
    )
