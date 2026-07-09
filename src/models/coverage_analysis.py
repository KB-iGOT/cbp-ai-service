import uuid
from sqlalchemy import Column, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from ..core.database import Base


class CoverageAnalysisResult(Base):
    """Latest curriculum coverage analysis for a role mapping — one row per
    (role_mapping_id, user_id), overwritten on every /coverage-analysis/analyze run."""
    __tablename__ = "coverage_analysis_results"
    __table_args__ = (
        UniqueConstraint("role_mapping_id", "user_id", name="uq_coverage_analysis_role_mapping_user"),
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False
    )

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    role_mapping_id = Column(
        UUID(as_uuid=True),
        ForeignKey("role_mappings.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    behavioural = Column(JSONB, nullable=False, default=list)
    functional = Column(JSONB, nullable=False, default=list)
    domain = Column(JSONB, nullable=False, default=list)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

    def __repr__(self):
        return f"<CoverageAnalysisResult(role_mapping_id={self.role_mapping_id}, user_id={self.user_id})>"
