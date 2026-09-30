from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings

settings = get_settings()

# Keyed by client IP address. In production behind a reverse proxy,
# this requires the proxy to correctly forward the real client IP
# (see the X-Forwarded-For / proxy headers note below) -- otherwise
# every request looks like it comes from the proxy's own IP, and rate
# limiting would apply globally instead of per-client.
#
# storage_uri points slowapi at Redis instead of its default in-memory
# store, so the counters are shared across every worker process and
# every replica of the API -- without this, running more than one
# uvicorn worker (or more than one container) silently multiplies the
# effective rate limit by the number of processes, since each one would
# keep its own separate count.
limiter = Limiter(key_func=get_remote_address, storage_uri=settings.redis_url)