"""
Storage service abstraction for documents
Supports local filesystem and GCP Cloud Storage
"""
import os
import uuid
from pathlib import Path
from typing import BinaryIO, Tuple
from abc import ABC, abstractmethod

from google.cloud import storage
from ..core.configs import settings, DocumentStorageOption
from ..core.logger import logger


class StorageService(ABC):
    """Abstract base class for storage services"""
    
    @abstractmethod
    def save_file(self, file_obj: BinaryIO, filename: str, state_center_id: str, 
                  department_id: str = None) -> Tuple[str, int]:
        """
        Save uploaded file and return (stored_path, file_size)
        stored_path should be usable to retrieve the file later
        """
        pass
    
    @abstractmethod
    def read_file(self, stored_path: str) -> bytes:
        """Read file content by stored path"""
        pass
    
    @abstractmethod
    def delete_file(self, stored_path: str) -> bool:
        """Delete file by stored path. Returns True if successful"""
        pass
    
    @abstractmethod
    def file_exists(self, stored_path: str) -> bool:
        """Check if file exists at stored path"""
        pass


class LocalStorageService(StorageService):
    """Local filesystem storage implementation"""
    
    def __init__(self, root_path: str):
        self.root_path = Path(root_path)
        self.root_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"LocalStorageService initialized with root: {self.root_path.resolve()}")
    
    def _build_path(self, state_center_id: str, department_id: str = None) -> Path:
        """Build directory path for organizing files"""
        subdir = self.root_path / str(state_center_id) / (str(department_id) if department_id else "_root_")
        subdir.mkdir(parents=True, exist_ok=True)
        return subdir
    
    def save_file(self, file_obj: BinaryIO, filename: str, state_center_id: str, 
                  department_id: str = None) -> Tuple[str, int]:
        """Save file to local filesystem"""
        # Generate unique filename to avoid conflicts
        file_uuid = uuid.uuid4()
        _, ext = os.path.splitext(filename)
        stored_filename = f"{file_uuid}{ext}"
        
        subdir = self._build_path(state_center_id, department_id)
        full_path = subdir / stored_filename
        
        # Save file
        size = 0
        with open(full_path, 'wb') as f:
            while chunk := file_obj.read(8192):  # 8KB chunks
                f.write(chunk)
                size += len(chunk)
        
        # Return relative path from root
        relative_path = full_path.relative_to(self.root_path)
        return str(relative_path), size
    
    def read_file(self, stored_path: str) -> bytes:
        """Read file from local filesystem"""
        full_path = self.root_path / stored_path
        if not full_path.exists():
            raise FileNotFoundError(f"File not found: {stored_path}")
        return full_path.read_bytes()
    
    def delete_file(self, stored_path: str) -> bool:
        """Delete file from local filesystem"""
        try:
            full_path = self.root_path / stored_path
            if full_path.exists():
                full_path.unlink()
                return True
            return False
        except Exception as e:
            logger.error(f"Error deleting file {stored_path}: {e}")
            return False
    
    def file_exists(self, stored_path: str) -> bool:
        """Check if file exists in local filesystem"""
        full_path = self.root_path / stored_path
        return full_path.exists()


class GCPStorageService(StorageService):
    """Google Cloud Storage implementation"""
    
    def __init__(self, bucket_name: str, prefix: str = "documents", credentials_path: str = None):
        self.bucket_name = bucket_name
        self.prefix = prefix.strip('/')  # Remove leading/trailing slashes
        
        # Initialize GCP client with specific credentials if provided
        if credentials_path and os.path.exists(credentials_path):
            logger.info(f"Using GCP Storage credentials from: {credentials_path}")
            self.client = storage.Client.from_service_account_json(credentials_path)
        else:
            logger.info("Using default GCP credentials for storage")
            self.client = storage.Client()
            
        self.bucket = self.client.bucket(bucket_name)
        logger.info(f"GCPStorageService initialized with bucket: {bucket_name}, prefix: {prefix}")
    
    def _build_blob_name(self, state_center_id: str, department_id: str = None, 
                         filename: str = None) -> str:
        """Build GCS object path"""
        parts = [self.prefix, str(state_center_id)]
        if department_id:
            parts.append(str(department_id))
        else:
            parts.append("_root_")
        if filename:
            parts.append(filename)
        return "/".join(parts)
    
    def save_file(self, file_obj: BinaryIO, filename: str, state_center_id: str, 
                  department_id: str = None) -> Tuple[str, int]:
        """Save file to GCP Cloud Storage"""
        # Generate unique filename
        file_uuid = uuid.uuid4()
        _, ext = os.path.splitext(filename)
        stored_filename = f"{file_uuid}{ext}"
        
        blob_name = self._build_blob_name(state_center_id, department_id, stored_filename)
        blob = self.bucket.blob(blob_name)
        
        # Upload file
        file_obj.seek(0)  # Reset to beginning
        content = file_obj.read()
        blob.upload_from_string(content, content_type='application/pdf')
        
        return blob_name, len(content)
    
    def read_file(self, stored_path: str) -> bytes:
        """Read file from GCP Cloud Storage"""
        blob = self.bucket.blob(stored_path)
        if not blob.exists():
            raise FileNotFoundError(f"File not found in GCS: {stored_path}")
        return blob.download_as_bytes()
    
    def delete_file(self, stored_path: str) -> bool:
        """Delete file from GCP Cloud Storage"""
        try:
            blob = self.bucket.blob(stored_path)
            if blob.exists():
                blob.delete()
                return True
            return False
        except Exception as e:
            logger.error(f"Error deleting file {stored_path}: {e}")
            return False
    
    def file_exists(self, stored_path: str) -> bool:
        """Check if file exists in GCP Cloud Storage"""
        blob = self.bucket.blob(stored_path)
        return blob.exists()


def get_storage_service() -> StorageService:
    """Factory function to get configured storage service"""
    if settings.DOCUMENT_STORAGE_TYPE == DocumentStorageOption.GCP:
        if not settings.GCP_STORAGE_BUCKET:
            raise ValueError("GCP_STORAGE_BUCKET must be set when using GCP storage")
        return GCPStorageService(
            bucket_name=settings.GCP_STORAGE_BUCKET,
            prefix=settings.GCP_STORAGE_PREFIX,
            credentials_path=settings.GCP_STORAGE_CREDENTIALS if settings.GCP_STORAGE_CREDENTIALS else None
        )
    else:
        return LocalStorageService(settings.DOCUMENT_STORAGE_ROOT)