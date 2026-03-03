import uuid
from io import BytesIO
from typing import List, Optional

from fastapi.responses import StreamingResponse
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, status, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.document import Document
from ...models.user import User

from ...core.configs import settings
from ...api.dependencies import get_current_active_user
from ...core.database import get_db_session

from ...schemas.document import BatchUploadResponse, DocumentResponse, DocumentListResponse, SummaryTriggerResponse, DocumentDeleteResponse, SummaryDeleteResponse, UploadFailure
from ...services.storage_service import get_storage_service
from ...prompts.prompts import DOCUMENT_SUMMARY_PROMPT
from ...crud.document import crud_document
from ...crud.meta_summary import crud_meta_summary

from google import genai
from google.genai import types

from ...core.logger import logger

# Lazy Gemini client init (reuse creds through GOOGLE_APPLICATION_CREDENTIALS env var)
_genai_client = None

def get_genai_client():
    global _genai_client
    if _genai_client is None:
        try:
            _genai_client = genai.Client(
                project=settings.GOOGLE_PROJECT_ID,
                location="us-central1",
                vertexai=True
            )
        except Exception as e:
            logger.error(f"Failed to init genai client: {e}")
            raise
    return _genai_client

router = APIRouter(prefix="/files", tags=["Documents"])

# Get storage service instance
storage_service = get_storage_service()

@router.post("", response_model=BatchUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_files(
    state_center_id: str = Form(...),
    department_id: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_active_user)
):
    """Upload multiple PDF files (1-10 files). Validation errors fail the entire request. Storage errors are tracked per file."""
    
    try:
        # Validate file count
        if not files or len(files) == 0:
            raise HTTPException(status_code=400, detail="At least 1 file is required")
        
        if len(files) > 10:
            raise HTTPException(status_code=400, detail="Maximum 10 files allowed per upload")
        
        total_files = len(files)
    
        # Validate ALL files first - any validation error fails the entire request
        validated_files = []
        for idx, file in enumerate(files):
            original_filename = file.filename
            
            # Check file extension
            if not original_filename.lower().endswith('.pdf'):
                raise HTTPException(
                    status_code=415, 
                    detail=f"File '{original_filename}' is not a PDF. Only PDF files are supported"
                )
            
            # Check for duplicates
            existing = await crud_document.get_by_state_center_and_department(
                db, state_center_id, original_filename, current_user.user_id, department_id
            )
            if existing:
                raise HTTPException(
                    status_code=409, 
                    detail=f"File '{original_filename}' already exists for this scope"
                )
            
            # Check file size
            file.file.seek(0, 2)
            size = file.file.tell()
            file.file.seek(0)
            
            if size > settings.PDF_MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=400, 
                    detail=f"File '{original_filename}' exceeds maximum allowed size of {settings.PDF_MAX_FILE_SIZE} bytes"
                )
            
            validated_files.append({
                'file': file,
                'filename': original_filename,
                'size': size
            })
        
        # All files passed validation - now attempt upload and save to DB
        successful_uploads = []
        failed_uploads = []
        
        for file_info in validated_files:
            try:
                # Save file using storage service
                stored_path, file_size = storage_service.save_file(
                    file_info['file'].file, 
                    file_info['filename'], 
                    state_center_id, 
                    department_id
                )

                # Save to database
                doc = Document(
                    file_id=uuid.uuid4(),
                    state_center_id=state_center_id,
                    department_id=department_id,
                    uploader_id=current_user.user_id,
                    filename=file_info['filename'],
                    stored_path=stored_path,
                    file_size_bytes=file_size,
                    summary_status="NOT_STARTED"
                )
                doc = await crud_document.create(db, doc)
                successful_uploads.append(doc)
                
            except Exception as e:
                logger.error(f"Error uploading/saving file '{file_info['filename']}': {e}", exc_info=True)
                failed_uploads.append(UploadFailure(
                    filename=file_info['filename'],
                    error=f"Upload/storage failed"
                ))
        
        # Determine status and message
        successful_count = len(successful_uploads)
        failed_count = len(failed_uploads)
        
        if successful_count == total_files:
            status_str = "complete"
            message = f"All {total_files} file(s) uploaded successfully"
        elif successful_count > 0:
            status_str = "partial"
            message = f"{successful_count} of {total_files} file(s) uploaded successfully, {failed_count} failed"
        else:
            status_str = "failed"
            message = f"All {total_files} file(s) failed to upload"
        
        return BatchUploadResponse(
            status=status_str,
            message=message,
            total_files=total_files,
            successful_count=successful_count,
            failed_count=failed_count,
            successful_uploads=successful_uploads,
            failed_uploads=failed_uploads
        )
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        logger.exception(f"Unexpected error in upload_files endpoint:")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to upload files"
        )

@router.get("", response_model=DocumentListResponse)
async def list_files(
    state_center_id: Optional[str] = Query(None),
    department_id: Optional[str] = Query(None),
    filename: Optional[str] = Query(None, description="Exact filename filter"),
    document_name: Optional[str] = Query(None, description="Exact document name filter"),
    uploader_id: Optional[uuid.UUID] = Query(None, description="Filter by uploader"),
    summary_status: Optional[str] = Query(None),
    include_summary: bool = Query(False),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_active_user)
):
    try:
        uploader_id = uploader_id if uploader_id else current_user.user_id
        total, docs = await crud_document.get_documents(
            db,
            summary_status,
            state_center_id,
            department_id,
            filename,
            document_name,
            uploader_id,
            include_summary,
            skip,
            limit
        )

        if not include_summary:
            return DocumentListResponse(items=docs, total=total).model_dump(
                exclude={"items": {"__all__": {"summary_text"}}}
            )

        return DocumentListResponse(items=docs, total=total)
    except Exception as e:
        logger.error(f"Error fetching list of files: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch documents"
        )

async def _run_document_summary(document_id: uuid.UUID):
    """Background task to generate summary for a document id."""
    
    try:
        logger.info(f"Document summary process started for {document_id}")
        doc: Document = await crud_document.get_by_id(document_id)
        if not doc:
            logger.warning(f"Document {document_id} disappeared before processing")
            return
        if doc.summary_status in ("COMPLETED", "IN_PROGRESS"):
            logger.info(f"Skipping summary generation for {document_id}, status={doc.summary_status}")
            return
        # Mark in progress
        update_records = {
            'summary_status': "IN_PROGRESS"
        }
        await crud_document.update(document_id, update_records)

        try:
            pdf_bytes = storage_service.read_file(doc.stored_path)
        except FileNotFoundError:
            update_records = {
                'summary_status': "FAILED",
                'summary_error': "File missing in storage"
            }
            await crud_document.update(document_id, update_records)
            return

        client = get_genai_client()
        try:
            contents = [
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                        types.Part.from_text(text=DOCUMENT_SUMMARY_PROMPT)
                    ]
                )
            ]

            generate_content_config = types.GenerateContentConfig(
                temperature=0.7,
                top_p=0.95,
                max_output_tokens=8192,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
                ],
            )

            response = await client.aio.models.generate_content(
                model="gemini-2.5-pro",
                contents=contents,
                config=generate_content_config
            )
            summary_text = response.text
            if not summary_text:
                raise RuntimeError("Empty summary returned by model")
            update_records = {
                'summary_text': summary_text,
                'summary_status': "COMPLETED",
                'summary_error': None
            }
            await crud_document.update(document_id, update_records)
            logger.info(f"Document summary process completed for {document_id}")
        except Exception as e:
            logger.exception("Summary generation failed")
            update_records = {
                'summary_status': "FAILED",
                'summary_error': str(e)
            }
            await crud_document.update(document_id, update_records)
    except Exception as e:
        logger.exception("document summary failed")

@router.post("/{file_id}/summary", response_model=SummaryTriggerResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_summary(
    file_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_active_user)
):
    """Trigger summary generation for a file. Idempotent behavior."""
    try:
        doc: Document = await crud_document.get_by_id(file_id)
        if not doc:
            raise HTTPException(status_code=404, detail="File not found")

        # If already processing or done, return current status (idempotent)
        if doc.summary_status in ("IN_PROGRESS", "COMPLETED"):
            request_id = doc.last_summary_request_id or uuid.uuid4()
            if doc.last_summary_request_id is None:
                await crud_document.update(file_id, {'last_summary_request_id': request_id})
            return SummaryTriggerResponse(file_id=doc.file_id, request_id=request_id, summary_status=doc.summary_status)

        request_id = uuid.uuid4()
        await crud_document.update(file_id, {
            'last_summary_request_id': request_id,
            'summary_status': 'NOT_STARTED'
        })

        background_tasks.add_task(_run_document_summary, doc.file_id)
        return SummaryTriggerResponse(file_id=doc.file_id, request_id=request_id, summary_status="IN_PROGRESS")
    except Exception as e:
        logger.error(f"Error while triggering summary: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to trigger summary"
        )

@router.delete("/{file_id}", response_model=DocumentDeleteResponse, status_code=status.HTTP_200_OK)
async def delete_file(
    file_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_active_user)
):
    """Delete a file and its storage. Cannot delete if summary is in progress."""
    doc: Document = await crud_document.get_by_id(file_id)
    if not doc:
        raise HTTPException(status_code=404, detail="File not found")

    # Prevent deletion if summary is in progress
    if doc.summary_status == "IN_PROGRESS":
        raise HTTPException(
            status_code=409, 
            detail="Cannot delete file while summary generation is in progress"
        )

    try:
        # Delete from storage
        storage_deleted = storage_service.delete_file(doc.stored_path)
        if not storage_deleted:
            logger.warning(f"Storage deletion failed for {doc.stored_path}, but continuing with DB cleanup")

        # Clean up meta-summaries that reference this file
        meta_summaries = await crud_meta_summary.get_by_identifiers(db, [str(file_id)])
        deleted_meta_summaries = []
        
        for meta in meta_summaries:
            # Remove the file_id from the array
            updated_file_ids = [fid for fid in meta.file_ids if fid != str(file_id)]
            
            if len(updated_file_ids) == 0:
                # If no files left, delete the meta-summary entirely
                await crud_meta_summary.delete_by_id(db, meta.request_id)
                deleted_meta_summaries.append(str(meta.request_id))
            else:
                # Update the file_ids array
                await crud_meta_summary.update(meta.request_id, {
                    'file_ids': updated_file_ids,
                    'status': 'PENDING',
                    'summary_text': None,
                    'error_message': None
                })

        await crud_document.delete_by_id(db, file_id)
        
        response_msg = "File deleted successfully"
        if deleted_meta_summaries:
            response_msg += f". Also deleted {len(deleted_meta_summaries)} meta-summaries that only referenced this file."
        
        return DocumentDeleteResponse(
            message=response_msg,
            file_id=file_id,
            filename=doc.filename,
            deleted_meta_summaries=deleted_meta_summaries
        )
        
    except Exception as e:
        logger.error(f"Error deleting file {file_id}: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete file")

@router.get("/{file_id}", response_model=DocumentResponse)
async def get_file(
    file_id: uuid.UUID,
    include_summary: bool = Query(False, description="Include summary text in response"),
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_active_user)
):
    """Get details of a single file"""
    doc: Document = await crud_document.get_by_id(file_id)
    if not doc:
        raise HTTPException(status_code=404, detail="File not found")
    
    # Optionally exclude summary text to reduce payload
    if not include_summary:
        doc.summary_text = None
    
    return doc

@router.delete("/{file_id}/summary", response_model=SummaryDeleteResponse, status_code=status.HTTP_200_OK)
async def delete_summary(
    file_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_active_user)
):
    """Delete only the summary of a file, keeping the file itself"""
    doc: Document = await crud_document.get_by_id(file_id)
    if not doc:
        raise HTTPException(status_code=404, detail="File not found")

    # Cannot delete summary if it's in progress
    if doc.summary_status == "IN_PROGRESS":
        raise HTTPException(
            status_code=409, 
            detail="Cannot delete summary while generation is in progress"
        )

    try:
        update_records = {
            'summary_text': None,
            'summary_status': "NOT_STARTED",
            'summary_error': None,
            'last_summary_request_id': None
        }
        await crud_document.update(file_id, update_records)
        return SummaryDeleteResponse(
            message="Summary deleted successfully. File remains available for new summary generation.",
            file_id=file_id,
            filename=doc.filename
        )
        
    except Exception as e:
        logger.error(f"Error deleting summary for file {file_id}: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete summary")
    
@router.get("/{file_id}/download", status_code=status.HTTP_200_OK)
async def download_file(
    file_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User =Depends(get_current_active_user)
):
    """Download a file by file_id (read bytes from storage_service)."""
    # Fetch document metadata
    # doc: Document = db.query(Document).filter(
    #     Document.file_id == file_id,
    #     Document.uploader_id == current_user.user_id
    #     ).first()
    doc: Document = await crud_document.get_by_id(file_id)
    if not doc:
        raise HTTPException(status_code=404, detail="File not found")

    try:
        # Read file bytes from storage service
        pdf_bytes = storage_service.read_file(doc.stored_path)
        if not pdf_bytes:
            raise HTTPException(status_code=404, detail="File missing. Please upload again!")

        # Wrap bytes in BytesIO for streaming
        file_like = BytesIO(pdf_bytes)

        return StreamingResponse(
            file_like,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'}
        )
    except Exception as e:
        logger.error(f"Error downloading file {file_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to download file")

