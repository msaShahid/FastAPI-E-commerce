import strawberry
from strawberry.extensions import MaxAliasesLimiter, QueryDepthLimiter
from strawberry.fastapi import GraphQLRouter

from app.graphql.context import get_graphql_context
from app.graphql.mutations.user import UserMutation
from app.graphql.queries.user import UserQuery
from app.shared.limits import MAX_GRAPHQL_ALIASES, MAX_GRAPHQL_DEPTH


@strawberry.type
class Query(UserQuery):
    pass


@strawberry.type
class Mutation(UserMutation):
    pass


schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=[
        # page_size is capped per field, but a client can ask for the same field
        # many times in one request with aliases (a1: users(...) a2: users(...)),
        # multiplying the cap. Bound both that and how deeply a query can nest.
        MaxAliasesLimiter(max_alias_count=MAX_GRAPHQL_ALIASES),
        QueryDepthLimiter(max_depth=MAX_GRAPHQL_DEPTH),
    ],
)


graphql_router = GraphQLRouter(
    schema,
    context_getter=get_graphql_context,
)