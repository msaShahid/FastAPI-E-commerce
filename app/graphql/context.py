from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from strawberry.fastapi import BaseContext

from app.core.database import async_session_factory
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
    authorization = request.headers.get("Authorization")

    if not authorization:
        raise ValueError("Missing Authorization header")

    scheme, _, token = authorization.partition(" ")

    if scheme.lower() != "bearer" or not token:
        raise ValueError("Invalid Authorization header")

    return HTTPAuthorizationCredentials(
        scheme=scheme,
        credentials=token,
    )


async def get_graphql_context(
    request: Request,
) -> GraphQLContext:
    """
     Uses the same JWT authentication and service layer as the REST API.
    """

    db = async_session_factory()

    try:
        credentials = get_bearer_credentials(request)

        # Authenticate using the existing REST authentication logic.
        auth_repository = AuthRepository(db)

        current_user = await get_current_user(
            credentials=credentials,
            repository=auth_repository,
        )

        # Build the same UserService used by REST.
        user_repository = UserRepository(db)
        user_service = UserService(user_repository)

        return GraphQLContext(
            request=request,
            db=db,
            current_user=current_user,
            user_service=user_service,
        )

    except Exception:
        await db.rollback()
        await db.close()
        raise
