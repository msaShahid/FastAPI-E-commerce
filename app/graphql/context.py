from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from strawberry.fastapi import BaseContext

from app.core.database import DbSession
from app.modules.auth.dependencies.auth import get_current_user
from app.modules.auth.models.user import User
from app.modules.auth.repositories.auth_repository import AuthRepository
from app.modules.users.repositories.user_repository import UserRepository
from app.modules.users.services.user_service import UserService


class GraphQLContext(BaseContext):
    def __init__(
        self,
        *,
        request: Request,
        db,
        current_user: User,
        user_service: UserService,
    ) -> None:
        super().__init__()

        self.request = request
        self.db = db
        self.current_user = current_user
        self.user_service = user_service


def get_bearer_credentials(
    request: Request,
) -> HTTPAuthorizationCredentials:
    # A missing or malformed header is an authentication failure, so it must
    # surface as a 401 -- same as the REST API's bearer dependency. A bare
    # ValueError here would escape as an unhandled exception (HTTP 500).
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )

    authorization = request.headers.get("Authorization")

    if not authorization:
        raise unauthorized

    scheme, _, token = authorization.partition(" ")

    if scheme.lower() != "bearer" or not token:
        raise unauthorized

    return HTTPAuthorizationCredentials(
        scheme=scheme,
        credentials=token,
    )


async def get_graphql_context(
    request: Request,
    db: DbSession,
) -> GraphQLContext:
    """
    Uses the same JWT authentication, database session lifecycle, and
    service layer as the REST API. FastAPI injects `get_db`, so the
    session is committed or rolled back and closed after the operation.
    """

    credentials = get_bearer_credentials(request)
    auth_repository = AuthRepository(db)
    current_user = await get_current_user(
        credentials=credentials,
        repository=auth_repository,
    )
    user_repository = UserRepository(db)
    user_service = UserService(user_repository)

    return GraphQLContext(
        request=request,
        db=db,
        current_user=current_user,
        user_service=user_service,
    )
