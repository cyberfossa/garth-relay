from fastapi import HTTPException, Request

from src.auth.session import get_current_user


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


async def require_user(request: Request, jwt_secret: str, jwt_algorithm: str) -> str:
    try:
        return await get_current_user(request, jwt_secret, jwt_algorithm)
    except HTTPException:
        if is_htmx(request):
            raise HTTPException(status_code=200, headers={"HX-Redirect": "/login"}) from None
        raise HTTPException(status_code=302, headers={"Location": "/login"}) from None
