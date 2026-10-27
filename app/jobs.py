"""
Imports every module that defines background job handlers, so they are
registered. The worker (scripts/run_worker.py) and the API's embedded worker
import this. When you add a job in another module, add its import here.
"""

import app.modules.auth.jobs  # noqa: F401
import app.modules.payments.jobs  # noqa: F401
