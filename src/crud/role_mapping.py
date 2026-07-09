import uuid
from typing import List, Optional
from sqlalchemy import and_, asc, delete, desc, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload, noload, contains_eager

# Assuming RoleMapping is defined in src/models/cbp_plan.py
from ..models.role_mapping import ProcessingStatus, RoleMapping
from ..models.designation_approval import DesignationApproval
from ..core.database import sessionmanager 

class CRUDRoleMapping:
    """
    CRUD methods for the RoleMapping model.
    """

    # Allowlist mapping client-facing sort keys to actual columns.
    # Prevents unsafe dynamic attribute access via getattr(RoleMapping, sort_by).
    SORTABLE_FIELDS = {
        "createdOn": RoleMapping.created_at,
        "updatedOn": RoleMapping.updated_at,
        "designationName": RoleMapping.designation_name,
        "sortOrder": RoleMapping.sort_order,
    }

    async def _get_by_id_in_session(self, db: AsyncSession, role_mapping_id: uuid.UUID) -> Optional[RoleMapping]:
        """Internal method to retrieve a record using an injected session."""
        stmt = select(RoleMapping).filter(RoleMapping.id == role_mapping_id)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_by_id(self, role_mapping_id: uuid.UUID) -> Optional[RoleMapping]:
        """
        Retrieves a RoleMapping record by its primary key ID, managing its own session.
        """
        async with sessionmanager.session() as db:
            return await self._get_by_id_in_session(db, role_mapping_id)

    async def get_by_id_and_user(
        self, 
        db: AsyncSession, 
        role_mapping_id: uuid.UUID, 
        user_id: uuid.UUID
    ) -> Optional[RoleMapping]:
        """
        Retrieves a RoleMapping record filtered by its ID and the associated user ID.

        Args:
            db: The async database session.
            role_mapping_id: The ID of the role mapping.
            user_id: The ID of the current user.

        Returns:
            The matching RoleMapping object or None if not found.
        """
        # Construct the SQLAlchemy 2.0 style select statement
        stmt = select(RoleMapping).filter(
            RoleMapping.id == role_mapping_id,
            RoleMapping.user_id == user_id
        )
        
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_all_mapping(
        self, 
        db: AsyncSession, 
        state_center_id: str, 
        user_id: uuid.UUID,
        department_id: Optional[str]
    ) -> Optional[RoleMapping]:
        """
        Checks for an existing RoleMapping record based on state_center_id, user_id, 
        and department_id (or department_id is NULL).

        Args:
            db: The async database session.
            state_center_id: The ID of the state center.
            user_id: The ID of the user (current_user.user_id).
            department_id: Optional ID of the department.
            
        Returns:
            The matching RoleMapping object, or None if not found.
        """
        
        conditions = [
            RoleMapping.state_center_id == state_center_id,
            RoleMapping.user_id == user_id
        ]
        
        # Apply conditional department filter (matching your request logic)
        if department_id:
            conditions.append(RoleMapping.department_id == department_id)
        else:
            # If department_id is None, we explicitly search for records where the column is NULL
            conditions.append(RoleMapping.department_id.is_(None))

        # Build the statement using sqlalchemy.future.select and sqlalchemy.and_
        stmt = select(RoleMapping).where(and_(*conditions)).order_by(desc(RoleMapping.sort_order)).limit(1)
        
        result = await db.execute(stmt)
        # Use scalars().one_or_none() for single-record retrieval
        return result.scalars().one_or_none()
    
    async def get_all_completed_mapping(
        self, 
        db: AsyncSession, 
        state_center_id: str, 
        user_id: uuid.UUID,
        department_id: Optional[str] = None,
        load_cbp_plans: bool = False
    ) -> Optional[List[RoleMapping]]:
        """
        Checks for an existing RoleMapping record based on state_center_id, user_id, 
        and department_id (or department_id is NULL).

        Args:
            db: The async database session.
            state_center_id: The ID of the state center.
            user_id: The ID of the user (current_user.user_id).
            department_id: Optional ID of the department.
            
        Returns:
            The matching RoleMapping object, or None if not found.
        """
        
        conditions = [
            RoleMapping.state_center_id == state_center_id,
            RoleMapping.user_id == user_id,
            RoleMapping.status == ProcessingStatus.COMPLETED # Mandatory status filter
        ]
        
        # Apply conditional department filter (matching your request logic)
        if department_id:
            conditions.append(RoleMapping.department_id == department_id)
        else:
            # If department_id is None, we explicitly search for records where the column is NULL
            conditions.append(RoleMapping.department_id.is_(None))

        # Build the statement using sqlalchemy.future.select and sqlalchemy.and_
        stmt = select(RoleMapping).where(and_(*conditions)).order_by(RoleMapping.sort_order)
        if load_cbp_plans:
            stmt = stmt.options(selectinload(RoleMapping.cbp_plans))
        else:
            # Prevent lazy loading by explicitly setting noload
            stmt = stmt.options(noload(RoleMapping.cbp_plans))
        
        # Always load only approved designation approvals
        stmt = stmt.outerjoin(
            DesignationApproval,
            and_(
                DesignationApproval.rolemapping_id == RoleMapping.id,
                DesignationApproval.status == 'pending' # Load only pending approvals for context in the UI
            )
        ).options(contains_eager(RoleMapping.designation_approvals))
        
        result = await db.execute(stmt)
        role_mappings = result.unique().scalars().all()
        if not load_cbp_plans:
            for mapping in role_mappings:
                # Use object.__setattr__ to bypass SQLAlchemy's descriptor
                object.__setattr__(mapping, 'cbp_plans', [])
        
        return role_mappings

    async def search(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        query: Optional[str] = None,
        state_center_id: Optional[str] = None,
        department_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        load_cbp_plans: bool = False,
        sort_by: Optional[dict] = None,
        match_status: Optional[str] = None
    ):
        """
        Search COMPLETED role mappings for the current user by designation name,
        with optional state/center and department filters.

        sort_by: e.g. {"createdOn": "desc"}. Falls back to sort_order ascending
        when omitted or the field isn't in SORTABLE_FIELDS.

        match_status: "matched" | "unmatched" | None. Filters only the returned
        page of `data` — the total/total_matched/total_unmatched counts always
        reflect the full filtered set regardless of this filter, so tab badges
        stay stable when switching tabs.

        Returns a tuple of (rows, total, total_matched, total_unmatched) where
        total/total_matched/total_unmatched are computed over the entire filtered
        result set (independent of limit/offset/match_status).
        """
        conditions = [
            RoleMapping.user_id == user_id,
            RoleMapping.status == ProcessingStatus.COMPLETED
        ]

        if state_center_id:
            conditions.append(RoleMapping.state_center_id == state_center_id)
        if department_id:
            conditions.append(RoleMapping.department_id == department_id)
        else:
            conditions.append(RoleMapping.department_id.is_(None))
        if query:
            conditions.append(RoleMapping.designation_name.ilike(f"%{query}%"))

        # Aggregate counts over the entire filtered set (not just the current page,
        # and NOT affected by match_status — counts must stay stable across tabs)
        count_stmt = select(
            func.count(RoleMapping.id),
            func.count(RoleMapping.id).filter(RoleMapping.igot_designation_id.isnot(None)),
            func.count(RoleMapping.id).filter(RoleMapping.igot_designation_id.is_(None))
        ).where(and_(*conditions))
        count_result = await db.execute(count_stmt)
        total, total_matched, total_unmatched = count_result.one()

        page_conditions = list(conditions)
        if match_status == "matched":
            page_conditions.append(RoleMapping.igot_designation_id.isnot(None))
        elif match_status == "unmatched":
            page_conditions.append(RoleMapping.igot_designation_id.is_(None))

        order_column = RoleMapping.sort_order
        order_direction = asc
        if sort_by:
            field, direction = next(iter(sort_by.items()))
            if field in self.SORTABLE_FIELDS:
                order_column = self.SORTABLE_FIELDS[field]
                order_direction = desc if str(direction).lower() == "desc" else asc

        stmt = (
            select(RoleMapping)
            .where(and_(*page_conditions))
            .order_by(order_direction(order_column))
            .limit(limit)
            .offset(offset)
        )
        if load_cbp_plans:
            stmt = stmt.options(selectinload(RoleMapping.cbp_plans))
        else:
            stmt = stmt.options(noload(RoleMapping.cbp_plans))

        stmt = stmt.outerjoin(
            DesignationApproval,
            and_(
                DesignationApproval.rolemapping_id == RoleMapping.id,
                DesignationApproval.status == 'pending'
            )
        ).options(contains_eager(RoleMapping.designation_approvals))

        result = await db.execute(stmt)
        role_mappings = result.unique().scalars().all()
        if not load_cbp_plans:
            for mapping in role_mappings:
                object.__setattr__(mapping, 'cbp_plans', [])

        return role_mappings, total, total_matched, total_unmatched

    async def get_reorder_list(
        self,
        db: AsyncSession,
        state_center_id: str,
        user_id: uuid.UUID,
        department_id: Optional[str] = None
    ) -> List[RoleMapping]:
        """
        Retrieves a lightweight list (id, designation_name, wing_division_section,
        sort_order) of COMPLETED role mappings, ordered by sort_order.
        Used to populate the drag-and-drop reorder UI without loading
        role/activity/competency/CBP plan data.
        """
        conditions = [
            RoleMapping.state_center_id == state_center_id,
            RoleMapping.user_id == user_id,
            RoleMapping.status == ProcessingStatus.COMPLETED
        ]

        if department_id:
            conditions.append(RoleMapping.department_id == department_id)
        else:
            conditions.append(RoleMapping.department_id.is_(None))

        stmt = (
            select(
                RoleMapping.id,
                RoleMapping.designation_name,
                RoleMapping.wing_division_section,
                RoleMapping.sort_order
            )
            .where(and_(*conditions))
            .order_by(RoleMapping.sort_order)
        )

        result = await db.execute(stmt)
        return result.all()

    async def update(
        self,
        role_mapping_id: uuid.UUID,
        update_records
    ) -> RoleMapping:
        
        stmt = (
            update(RoleMapping)
            .where(RoleMapping.id == role_mapping_id)
            .values(**update_records)
            .returning(RoleMapping)
        )
        async with sessionmanager.session() as db:
            result = await db.execute(stmt)
            await db.commit()
            updated_record = result.scalar_one()
            return updated_record
        
    async def create(
        self, 
        new_mappings: List[RoleMapping]
    ) -> List[RoleMapping]:
        async with sessionmanager.session() as db:
            db.add_all(new_mappings)
            await db.commit()
            for mapping in new_mappings:
                await db.refresh(mapping)
            return new_mappings

    async def create_with_next_sort_order(
        self,
        new_mappings: List[RoleMapping],
        state_center_id: str,
        user_id: uuid.UUID,
        department_id: Optional[str]
    ) -> List[RoleMapping]:
        """
        Atomically assigns the next sort_order(s) and creates the role mapping rows.

        Uses SELECT … FOR UPDATE to lock the existing rows for the given
        user/state_center/department scope, preventing two concurrent requests
        from computing the same MAX(sort_order) and producing duplicates.
        """
        conditions = [
            RoleMapping.state_center_id == state_center_id,
            RoleMapping.user_id == user_id,
        ]
        if department_id:
            conditions.append(RoleMapping.department_id == department_id)
        else:
            conditions.append(RoleMapping.department_id.is_(None))

        async with sessionmanager.session() as db:
            # Lock existing rows so concurrent requests queue up here
            lock_stmt = (
                select(RoleMapping.id)
                .where(and_(*conditions))
                .with_for_update()
            )
            await db.execute(lock_stmt)

            # Compute the next available sort_order within the lock
            max_stmt = select(
                func.coalesce(func.max(RoleMapping.sort_order), 0)
            ).where(and_(*conditions))
            result = await db.execute(max_stmt)
            current_max = result.scalar()

            for i, mapping in enumerate(new_mappings):
                mapping.sort_order = current_max + 1 + i

            db.add_all(new_mappings)
            await db.commit()
            for mapping in new_mappings:
                await db.refresh(mapping)
            return new_mappings

    async def get_in_progress_mapping(
        self, 
        db: AsyncSession, 
        state_center_id: str, 
        user_id: uuid.UUID,
        department_id: Optional[str]
    ) -> Optional[RoleMapping]:
        """
        Retrieves a RoleMapping record that is currently marked as IN_PROGRESS 
        for the given user and context.
        
        Args:
            db: The async database session.
            state_center_id: The ID of the state center.
            user_id: The ID of the user.
            department_id: Optional ID of the department.
            
        Returns:
            The matching RoleMapping object in IN_PROGRESS status, or None.
        """
        
        conditions = [
            RoleMapping.state_center_id == state_center_id,
            RoleMapping.user_id == user_id,
            RoleMapping.status == ProcessingStatus.IN_PROGRESS # Mandatory status filter
        ]
        
        # Apply conditional department filter
        if department_id:
            conditions.append(RoleMapping.department_id == department_id)
        else:
            # If department_id is None, explicitly search for records where the column is NULL
            conditions.append(RoleMapping.department_id.is_(None))

        stmt = (
            select(RoleMapping)
            .where(and_(*conditions))
            .limit(1) # We only need one match
        )
        
        result = await db.execute(stmt)
        return result.scalars().one_or_none()

    async def delete_existing_mappings(
        self, 
        db: AsyncSession, 
        state_center_id: str, 
        user_id: uuid.UUID,
        department_id: Optional[str]
    ) -> int:
        """
        Deletes all RoleMapping records matching the given user, state center, 
        and department context (or lack thereof). 
        This uses the SQLAlchemy 2.0 style async delete operation.

        Args:
            db: The async database session.
            state_center_id: The ID of the state center.
            user_id: The ID of the user.
            department_id: Optional ID of the department.
            
        Returns:
            The number of rows deleted.
        """
        conditions = [
            RoleMapping.state_center_id == state_center_id,
            RoleMapping.user_id == user_id
        ]

        if department_id:
            conditions.append(RoleMapping.department_id == department_id)
        else:
            conditions.append(RoleMapping.department_id.is_(None))
            
        # Build the delete statement
        stmt = delete(RoleMapping).where(and_(*conditions))
        
        # Execute the statement
        result = await db.execute(stmt)
        
        # Commit the transaction to finalize deletion
        await db.commit()
        
        return result.rowcount
    
    async def delete_by_id(
        self,
        db: AsyncSession,
        role_mapping_id: uuid.UUID
    ) -> int:
        conditions = [
            RoleMapping.id == role_mapping_id
        ]

        # Build the delete statement
        stmt = delete(RoleMapping).where(and_(*conditions))

        # Execute the statement
        result = await db.execute(stmt)

        # Commit the transaction to finalize deletion
        await db.commit()

        return result.rowcount

    async def bulk_update_sort_order(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        order_updates: List[dict]
    ) -> int:
        """
        Bulk update sort_order for multiple role mappings.
        Used for drag-and-drop reordering of designations.

        Args:
            db: The async database session.
            user_id: The ID of the current user (for ownership verification).
            order_updates: List of dicts with 'id' and 'sort_order' keys.

        Returns:
            The number of rows updated.
        """
        updated_count = 0

        for item in order_updates:
            stmt = (
                update(RoleMapping)
                .where(
                    and_(
                        RoleMapping.id == item['id'],
                        RoleMapping.user_id == user_id
                    )
                )
                .values(sort_order=item['sort_order'])
            )
            result = await db.execute(stmt)
            updated_count += result.rowcount

        await db.commit()
        return updated_count
    
    async def bulk_update_designation_matching(
    self,
    db: AsyncSession,
    updates: List[dict]
    ) -> int:
        """
        Bulk update designation matching data.
        
        Args:
            updates: List of dicts with 'role_mapping_id', 'igot_designation_name',
                    'igot_designation_id', and optionally 'designation_name'
        """
        if not updates:
            return 0

        updated_count = 0
        for update_item in updates:
            role_mapping_id = update_item.pop('role_mapping_id')
            stmt = update(RoleMapping).where(RoleMapping.id == role_mapping_id).values(**update_item)
            result = await db.execute(stmt)
            updated_count += result.rowcount

        await db.commit()
        return updated_count


# Initialize the CRUD utility for use across the application
crud_role_mapping = CRUDRoleMapping()