from typing import Optional, Union, Dict, Any
from datetime import datetime
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update
from sqlalchemy.orm import Session, joinedload

from ..models.user import User
from ..schemas.user import UserUpdate

class CRUDUser:
    """
    CRUD methods for the User model, supporting asynchronous operations.
    """
    
    async def get_by_id(self, db: AsyncSession, id: int) -> Optional[User]:
        """Retrieve a user by their primary key ID."""
        result = await db.execute(select(User).filter(User.user_id == id))
        return result.scalars().first()

    async def get_by_username(self, db: AsyncSession, username: str) -> Optional[User]:
        """Retrieve a user by their unique username."""
        result = await db.execute(select(User).filter((User.username == username) | (User.email == username)))
        return result.scalars().first()

    async def get_by_email(self, db: AsyncSession, email: str) -> Optional[User]:
        """Retrieve a user by their unique email."""
        result = await db.execute(select(User).filter(User.email == email))
        return result.scalars().first()
    
    async def get_by_id_with_relations(self, session: AsyncSession, user_id: int) -> Optional[User]:
        """
        Retrieve a single User by ID, explicitly loading 'addresses' and 'posts'
        relationships using the selectinload strategy to avoid N+1 queries.

        This is the method that was recently updated/discussed.
        """
        # The selectinload pattern is generally preferred for collections in async
        # environments as it fetches related data in one extra, optimized query.
        stmt = (
            select(User)
            .where(User.user_id == user_id)
            .options(
                joinedload(User.role),
                joinedload(User.creator)
            )
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.scalar_one()

    async def create(self, db: AsyncSession, db_obj: User) -> User:
        """Create a new user record."""
        # The password in obj_in.password must be HASHED before this point.
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def update(
        self, 
        db: AsyncSession,
        user_id: uuid.UUID,
        obj_in: UserUpdate
    ) -> User:
        """Update an existing user record."""
        update_data = obj_in.model_dump(exclude_unset=True)
        stmt = (
            update(User)
            .where(User.user_id == user_id)
            .values(**update_data)
            .returning(User)
        )
        result = await db.execute(stmt)
        await db.commit()
        return result.scalar_one()

    async def update_last_login(self, db: AsyncSession, user: User) -> User:
        """
        Specific CRUD operation for updating the user's last login timestamp.
        (Called directly by the authentication flow).
        """
        user.last_login = datetime.now()
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

# Initialize the CRUD utility for use across the application
crud_user = CRUDUser()