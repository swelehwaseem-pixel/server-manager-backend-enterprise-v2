from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_async_db
from app.models.user import User
from app.models.role import Role, ROLE_HIERARCHY


pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
)

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/v1/auth/login"
)


class SecurityUtils:

    @staticmethod
    def hash_password(password: str) -> str:
        return pwd_context.hash(password)

    @staticmethod
    def verify_password(
        plain_password: str,
        hashed_password: str,
    ) -> bool:
        return pwd_context.verify(
            plain_password,
            hashed_password,
        )

    @staticmethod
    def create_access_token(
        data: dict,
        expires_delta: Optional[timedelta] = None,
    ) -> str:
        to_encode = data.copy()

        expire = (
            datetime.now(timezone.utc)
            + (
                expires_delta
                or timedelta(
                    minutes=settings.access_token_expire_minutes
                )
            )
        )

        to_encode.update({"exp": expire})

        return jwt.encode(
            to_encode,
            settings.secret_key,
            algorithm=settings.algorithm,
        )


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_async_db),
) -> User:

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={
            "WWW-Authenticate": "Bearer",
        },
    )

    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm],
        )

        username: Optional[str] = payload.get("sub")

        if not username:
            raise credentials_exception

    except JWTError:
        raise credentials_exception

    result = await db.execute(
        select(User).filter(
            User.username == username
        )
    )

    user = result.scalars().first()

    if user is None:
        raise credentials_exception

    return user


def require_role(required_role: Role):

    async def role_dependency(
        current_user: User = Depends(get_current_user),
    ) -> User:

        try:
            current_role = Role(current_user.role)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User has an invalid system role.",
            )

        allowed_roles = ROLE_HIERARCHY.get(
            current_role,
            set(),
        )

        if required_role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Insufficient privileges. "
                    f"Required role: {required_role.value}."
                ),
            )

        return current_user

    return role_dependency


# Convenient dependencies for routers/endpoints

require_viewer = require_role(Role.VIEWER)

require_operator = require_role(Role.OPERATOR)

require_dba = require_role(Role.DBA)

require_admin = require_role(Role.ADMIN)
