"""Auth simple par token pour l'API admin."""
from fastapi import Header, HTTPException, status

from ..config import get_settings


async def require_admin(x_admin_token: str = Header(..., alias="X-Admin-Token")):
    settings = get_settings()
    if x_admin_token != settings.admin_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token")
    return True
