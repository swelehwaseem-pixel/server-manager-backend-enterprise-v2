from fastapi import Depends, HTTPException, status
from app.models.role import Role, ROLE_HIERARCHY
from app.core.security import get_current_user  # your existing JWT-decoding dependency


def require_role(minimum_role: Role):
    def dependency(current_user=Depends(get_current_user)):
        user_role = Role(current_user.role)
        if minimum_role not in ROLE_HIERARCHY[user_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires '{minimum_role.value}' role or higher",
            )
        return current_user
    return dependency
