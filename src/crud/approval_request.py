import uuid
from typing import List, Optional, Dict
from sqlalchemy import and_, update, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import update as sa_update
from sqlalchemy import or_, cast, String

from datetime import datetime, time as dtime

from ..models.approval_request import RequestedRoleMapping, RequestedRoleMappingItem, RequestStatus
from ..models.role_mapping import RoleMapping


class CRUDApprovalRequest:
    """
    CRUD methods for the RequestedRoleMapping and RequestedRoleMappingItem models.
    """
    
    async def create(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        request_name: str,
        mdo_admin_id: str,
        mdo_admin_name: str,
        state_center_id: str,
        state_center_name: str,
        department_id: Optional[str],
        department_name: Optional[str],
        role_mapping_snapshots: List[Dict]
    ) -> RequestedRoleMapping:
        """
        Create a new approval request with role mapping snapshots in a transaction.
        
        Args:
            db: The async database session
            user_id: ID of the user creating the request
            request_name: Name of the approval request
            mdo_admin_id: ID of the MDO admin/leader
            mdo_admin_name: Name of the MDO admin/leader
            state_center_id: ID of the state/center
            state_center_name: Name of the state/center
            department_id: Optional ID of the department
            department_name: Optional name of the department
            role_mapping_snapshots: List of role mapping data to snapshot
            
        Returns:
            The created RequestedRoleMapping object
        """
        # Create the approval request
        approval_request = RequestedRoleMapping(
            user_id=user_id,
            request_name=request_name,
            mdo_admin_id=mdo_admin_id,
            mdo_admin_name=mdo_admin_name,
            state_center_id=state_center_id,
            state_center_name=state_center_name,
            department_id=department_id,
            department_name=department_name or "",
            designation_count=len(role_mapping_snapshots),
            request_status=RequestStatus.PENDING.value
        )
        
        db.add(approval_request)
        await db.flush()  # Flush to get the request_id
        
        # Create role mapping item snapshots
        for snapshot in role_mapping_snapshots:
            item = RequestedRoleMappingItem(
                request_id=approval_request.request_id,
                original_role_mapping_id=snapshot['id'],
                designation_name=snapshot['designation_name'],
                wing_division_section=snapshot.get('wing_division_section'),
                role_responsibilities=snapshot.get('role_responsibilities', []),
                activities=snapshot.get('activities', []),
                competencies=snapshot.get('competencies', []),
                igot_designation_name=snapshot['igot_designation_name'],
                igot_designation_id=snapshot['igot_designation_id'],
                sort_order=snapshot.get('sort_order'),
                cbp_plans=snapshot.get('cbp_plans', [])
            )
            db.add(item)
        
        await db.commit()
        await db.refresh(approval_request)
        
        return approval_request
    
    async def get_by_id(
        self,
        db: AsyncSession,
        request_id: uuid.UUID
    ) -> Optional[RequestedRoleMapping]:
        """
        Retrieve an approval request by ID with role mapping items eagerly loaded.
        
        Args:
            db: The async database session
            request_id: The ID of the approval request
            
        Returns:
            The RequestedRoleMapping object or None if not found
        """
        stmt = (
            select(RequestedRoleMapping)
            .options(selectinload(RequestedRoleMapping.role_mapping_items))
            .filter(RequestedRoleMapping.request_id == request_id)
        )
        
        result = await db.execute(stmt)
        return result.scalars().first()
    
    async def get_by_user(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        state_center_id: Optional[str] = None,
        department_id: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date=None,
        to_date=None,
        page: int = 1,
        page_size: int = 10
    ) -> List[RequestedRoleMapping]:
        """
        Retrieve all approval requests for a user with optional filters and pagination.
        Results are sorted by created_at in descending order (newest first).
        
        Args:
            db: The async database session
            user_id: The ID of the user
            state_center_id: Optional filter by state/center ID
            department_id: Optional filter by department ID
            status: Optional filter by request status
            search: Optional search by request name (partial) or request ID (exact)
            time_filter: Optional filter by time period (today, last_week, last_month, last_3_months)
            page: Page number (default: 1)
            page_size: Items per page (default: 10)
            
        Returns:
            List of RequestedRoleMapping objects
        """
        
        
        conditions = [RequestedRoleMapping.user_id == user_id]
        
        if state_center_id:
            conditions.append(RequestedRoleMapping.state_center_id == state_center_id)
        
        if department_id:
            conditions.append(RequestedRoleMapping.department_id == department_id)
        
        if status:
            conditions.append(RequestedRoleMapping.request_status == status)
        
        # Search by request name (partial match) OR request ID (partial match)
        if search:
            from sqlalchemy import cast, String
            
            search_conditions = []
            
            # Search by request name (case-insensitive partial match)
            search_conditions.append(RequestedRoleMapping.request_name.ilike(f"%{search}%"))
            
            # Search by request ID (partial match by casting UUID to string)
            search_conditions.append(cast(RequestedRoleMapping.request_id, String).ilike(f"%{search}%"))
            
            # Combine search conditions with OR
            conditions.append(or_(*search_conditions))
        
        # Date range filtering
        if from_date:
            conditions.append(RequestedRoleMapping.created_at >= datetime.combine(from_date, dtime.min))
        
        if to_date:
            conditions.append(RequestedRoleMapping.created_at <= datetime.combine(to_date, dtime.max))
        
        # Calculate offset for pagination
        offset = (page - 1) * page_size
        
        stmt = (
            select(RequestedRoleMapping)
            .where(and_(*conditions))
            .order_by(desc(RequestedRoleMapping.created_at))
            .offset(offset)
            .limit(page_size)
        )
        
        result = await db.execute(stmt)
        return result.scalars().all()
    
    async def update_status(
        self,
        db: AsyncSession,
        request_id: uuid.UUID,
        new_status: str
    ) -> Optional[RequestedRoleMapping]:
        """
        Update the status of an approval request.
        The updated_at timestamp is automatically updated by the database.
        
        Args:
            db: The async database session
            request_id: The ID of the approval request
            new_status: The new status value
            
        Returns:
            The updated RequestedRoleMapping object or None if not found
        """
        stmt = (
            update(RequestedRoleMapping)
            .where(RequestedRoleMapping.request_id == request_id)
            .values(request_status=new_status)
            .returning(RequestedRoleMapping)
        )
        
        result = await db.execute(stmt)
        await db.commit()
        
        updated_request = result.scalar_one_or_none()
        if updated_request:
            await db.refresh(updated_request)
        
        return updated_request
    
    async def delete(
        self,
        db: AsyncSession,
        request_id: uuid.UUID
    ) -> bool:
        """
        Delete an approval request and its role mapping items.
        Child items are removed via cascade delete on the model relationship.
        
        Args:
            db: The async database session
            request_id: The ID of the approval request to delete
            
        Returns:
            True if deleted, False if not found
        """
        stmt = select(RequestedRoleMapping).where(RequestedRoleMapping.request_id == request_id)
        result = await db.execute(stmt)
        request = result.scalars().first()
        
        if not request:
            return False
        
        await db.delete(request)
        await db.commit()
        return True
    
    async def get_item_by_id(
        self,
        db: AsyncSession,
        request_id: uuid.UUID,
        item_id: uuid.UUID
    ) -> Optional[RequestedRoleMappingItem]:
        """
        Retrieve a single RequestedRoleMappingItem by its ID, scoped to the parent request.
        
        Args:
            db: The async database session
            request_id: Parent approval request ID
            item_id: The role mapping item ID
            
        Returns:
            The RequestedRoleMappingItem or None if not found
        """
                
        stmt = select(RequestedRoleMappingItem).where(
            and_(
                or_(
                    RequestedRoleMappingItem.id == item_id,
                    RequestedRoleMappingItem.original_role_mapping_id == item_id
                ),
                RequestedRoleMappingItem.request_id == request_id
            )
        )
        result = await db.execute(stmt)
        return result.scalars().first()
    
    async def update_cbp_plans(
        self,
        db: AsyncSession,
        request_id: uuid.UUID,
        item_id: uuid.UUID,
        cbp_plans: List[Dict]
    ) -> Optional[RequestedRoleMappingItem]:
        """
        Replace the cbp_plans on a RequestedRoleMappingItem.
        
        Args:
            db: The async database session
            request_id: Parent approval request ID (for scoping)
            item_id: The role mapping item ID to update
            cbp_plans: New list of CBP plan dicts
            
        Returns:
            The updated RequestedRoleMappingItem or None if not found
        """

        
        stmt = (
            sa_update(RequestedRoleMappingItem)
            .where(
                and_(
                    or_(
                        RequestedRoleMappingItem.id == item_id,
                        RequestedRoleMappingItem.original_role_mapping_id == item_id
                    ),
                    RequestedRoleMappingItem.request_id == request_id
                )
            )
            .values(cbp_plans=cbp_plans)
            .returning(RequestedRoleMappingItem)
        )
        
        result = await db.execute(stmt)
        await db.commit()
        
        updated = result.scalars().first()
        if updated:
            await db.refresh(updated)
        return updated
    
    async def get_role_mappings_by_ids(
        self,
        db: AsyncSession,
        role_mapping_ids: List[uuid.UUID],
        user_id: uuid.UUID
    ) -> List[RoleMapping]:
        """
        Retrieve role mappings by IDs, filtered by user ownership.
        Eagerly loads recommended_courses relationship.
        
        Args:
            db: The async database session
            role_mapping_ids: List of role mapping IDs
            user_id: The ID of the user (for ownership verification)
            
        Returns:
            List of RoleMapping objects with recommended_courses loaded
        """
        stmt = (
            select(RoleMapping)
            .options(
                selectinload(RoleMapping.recommended_courses),
                selectinload(RoleMapping.cbp_plans)
            )
            .where(
                and_(
                    RoleMapping.id.in_(role_mapping_ids),
                    RoleMapping.user_id == user_id
                )
            )
        )
        
        result = await db.execute(stmt)
        return result.scalars().all()


# Initialize the CRUD utility for use across the application
crud_approval_request = CRUDApprovalRequest()
