from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds standard defensive headers to every response. Each one guards
    against a specific, well-known class of attack:

    X-Content-Type-Options: nosniff
        Stops browsers from guessing a response's content type and
        executing it as something more dangerous than intended (e.g.
        treating a JSON response as executable HTML/JS).

    X-Frame-Options: DENY
        Prevents this API's responses from being embedded in an
        <iframe> on another site -- guards against clickjacking.

    Strict-Transport-Security
        Tells browsers to only ever connect via HTTPS for this domain,
        for the given duration. Only meaningful once real HTTPS is
        actually in front of this app (a reverse proxy/load balancer --
        seed Stage 12's note that this app doesn't terminate HTTPS
        itself). Harmless to send even before that's in place.

    Referrer-Policy: strict-origin-when-cross-origin
        Limits how much of the URL leaks to third-party sites via the
        Referer header when a user clicks a link away from this app.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response