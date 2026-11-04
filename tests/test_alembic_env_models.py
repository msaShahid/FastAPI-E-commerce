"""alembic/env.py registers models by hand. A model missing from it makes `alembic check` (and autogenerate) report its table as
"removed" even though the migration exists. This fails here, naming the model, instead of at deploy time."""

import pathlib

import pytest

import app.main  # noqa: F401  (importing the application imports every router and so every model)
from app.core.database import Base

ENV = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "env.py"


def test_every_model_is_imported_by_alembic_env():
    if not ENV.exists():
        pytest.skip("alembic/env.py is not part of this environment")
    text = ENV.read_text()
    missing = sorted(
        f"{mapper.class_.__module__}.{mapper.class_.__name__}"
        for mapper in Base.registry.mappers
        if f"from {mapper.class_.__module__} import" not in text
    )
    assert not missing, f"alembic/env.py does not import: {missing}"
