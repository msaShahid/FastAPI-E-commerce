import strawberry
from strawberry.fastapi import GraphQLRouter

from app.graphql.context import get_graphql_context
from app.graphql.queries.user import UserQuery


@strawberry.type
class Query(UserQuery):
    pass


schema = strawberry.Schema(
    query=Query,
)


graphql_router = GraphQLRouter(
    schema,
    context_getter=get_graphql_context,
)