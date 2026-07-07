import uuid
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from ..models.coverage_analysis import CoverageAnalysisResult


class CRUDCoverageAnalysis:
    async def get_by_role_mapping_id(
        self,
        db: AsyncSession,
        role_mapping_id: uuid.UUID,
        user_id: uuid.UUID
    ) -> Optional[CoverageAnalysisResult]:
        stmt = select(CoverageAnalysisResult).filter(
            CoverageAnalysisResult.role_mapping_id == role_mapping_id,
            CoverageAnalysisResult.user_id == user_id,
        )
        result = await db.execute(stmt)
        return result.scalars().first()

    async def upsert(
        self,
        db: AsyncSession,
        role_mapping_id: uuid.UUID,
        user_id: uuid.UUID,
        behavioural: list,
        functional: list,
        domain: list,
    ) -> CoverageAnalysisResult:
        """One row per (role_mapping_id, user_id) — overwrites the previous analysis in place
        rather than accumulating history."""
        existing = await self.get_by_role_mapping_id(db, role_mapping_id, user_id)
        if existing:
            existing.behavioural = behavioural
            existing.functional = functional
            existing.domain = domain
            await db.commit()
            await db.refresh(existing)
            return existing

        new_row = CoverageAnalysisResult(
            id=uuid.uuid4(),
            role_mapping_id=role_mapping_id,
            user_id=user_id,
            behavioural=behavioural,
            functional=functional,
            domain=domain,
        )
        db.add(new_row)
        await db.commit()
        await db.refresh(new_row)
        return new_row


crud_coverage_analysis = CRUDCoverageAnalysis()
