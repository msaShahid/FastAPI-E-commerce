from slowapi import Limiter
from slowapi.util import get_remote_address

# Keyed by client IP address. In production behind a reverse proxy,
# this requires the proxy to correctly forward the real client IP
# (see the X-Forwarded-For / proxy headers note below) -- otherwise
# every request looks like it comes from the proxy's own IP, and rate
# limiting would apply globally instead of per-client.
limiter = Limiter(key_func=get_remote_address)