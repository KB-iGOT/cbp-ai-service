from sqlalchemy import Column, String, DateTime, Text, Index
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector

from ..core.database import Base


class DesignationEmbedding(Base):
    """Stores iGOT master designations with their gemini-embedding-2 vectors."""
    __tablename__ = "designation_embeddings"

    id          = Column(String,      primary_key=True, nullable=False, comment="iGOT designation ID")
    designation = Column(String,   index=True,   nullable=False,   comment="Original designation name")
    content     = Column(Text,  nullable=False,   comment="Formatted text passed to the embedding model")
    embedding   = Column(Vector(768), nullable=False,   comment="gemini-embedding-2 vector (768 dims)")
    created_at  = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at  = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_designation_name_lower", func.lower(designation)),
    )

    def __repr__(self):
        return f"<DesignationEmbedding(id={self.id!r}, designation={self.designation!r})>"
