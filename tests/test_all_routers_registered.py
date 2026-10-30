"""Every router the code defines must be registered in app/main.py. A router that is written but not included fails silently: its
endpoints answer 404 (and, for a module whose models only that router imports, its tables are never created). This test makes that loud."""

import importlib
from pathlib import Path

from fastapi import APIRouter

import app.modules as modules
from app.core.config import get_settings
from app.main import app


def test_every_router_defined_in_a_module_is_registered_in_the_application():
    published = {(path, method.upper()) for path, operations in app.openapi()["paths"].items() for method in operations}
    prefix = get_settings().api_v1_prefix
    unregistered, scanned = [], 0
    root = Path(modules.__path__[0])
    # (files are found on disk, not with pkgutil: several packages here have no __init__.py and pkgutil would skip them)
    for file in sorted(root.glob("*/api/*.py")):
        if file.name == "__init__.py":
            continue
        info = type("Info", (), {"name": f"app.modules.{file.parent.parent.name}.api.{file.stem}"})
        module = importlib.import_module(info.name)
        for name, router in vars(module).items():
            if not isinstance(router, APIRouter) or router.__module__ != "fastapi.routing" or not router.routes:
                continue
            if getattr(router, "_registration_checked", False):
                continue
            scanned += 1
            for route in router.routes:
                missing = [m for m in (route.methods or []) if (prefix + route.path, m) not in published]
                if missing:
                    unregistered.append(f"{info.name}.{name}: {missing} {prefix + route.path}")
    assert scanned >= 20, f"only {scanned} routers found: the scan itself is broken"
    assert not unregistered, "routers defined but not registered in app/main.py (add app.include_router(...)):\n" + "\n".join(unregistered[:20])
