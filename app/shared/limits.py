"""
Upper bounds that keep any single request from returning, or making the
database do, an unbounded amount of work. tests/integration/test_pagination_bounds.py
enforces them across every endpoint.
"""

# Rows in one page of any list endpoint (REST and GraphQL).
MAX_PAGE_SIZE = 100

# Highest page number. Pages are OFFSET-based, and the database still walks
# every skipped row, so an unbounded page is an unbounded query (and past
# about 9e16 the OFFSET overflows a 64-bit integer and used to be an HTTP 500).
# 10,000 pages of 100 = 1,000,000 rows deep, which is far past anything a person
# browses; to reach older data, filter (category, status, date ...) instead.
MAX_PAGE = 10_000

# Collections that are not paginated are capped instead.
MAX_ADDRESSES_PER_USER = 20
MAX_CART_LINES = 50  # distinct products in one cart

# GraphQL: page_size is capped per field, but one request can repeat a field with
# aliases, and nested fields can multiply work. Bound both per request.
MAX_GRAPHQL_ALIASES = 10
MAX_GRAPHQL_DEPTH = 10
