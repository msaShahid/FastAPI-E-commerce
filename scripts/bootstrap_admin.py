"""
Create (or promote) the very first admin user.

`scripts/seed.py` refuses to run when ENVIRONMENT=production, on purpose --
it creates fixed, publicly-known test credentials that must never exist on
a real deployment. But that leaves a real gap: the API itself has no way
to create an admin. Registration (`POST /auth/register`) always creates a
regular user, and promoting a user to ADMIN via `PATCH /users/{id}`
requires you to *already* be an admin (see UserService.update_user). On a
fresh production database there is no admin yet, so nothing can grant the
first one through the API. This script is the deliberate, human-run
escape hatch for that specific chicken-and-egg problem -- unlike seed.py,
it is safe (and meant) to run against production.

Usage (inside the API container, so it shares the app's DATABASE_URL):

    docker compose exec api python -m scripts.bootstrap_admin --email admin@yourcompany.com

    # non-interactive (e.g. a one-off deploy step in CI/CD):
    ADMIN_BOOTSTRAP_PASSWORD='...' \\
        docker compose exec -T api python -m scripts.bootstrap_admin \\
        --email admin@yourcompany.com --username admin --yes

What it does:
    - Email already exists and is already an admin  -> no-op, just reports it.
    - Email already exists but is NOT an admin       -> promotes that user to
      ADMIN in place. No password is touched; --username is ignored (the
      account keeps its existing username).
    - Email does not exist yet                       -> creates the user
      (via AuthService.register, so it gets a properly hashed password and
      the same email/username uniqueness checks the API itself enforces),
      then immediately promotes it to ADMIN.

Password handling:
    - Never passed as a bare CLI argument (it would land in shell history
      and be visible to anyone on the box via `ps`).
    - Read from the ADMIN_BOOTSTRAP_PASSWORD environment variable if set
      (for non-interactive/CI use -- the caller is responsible for not
      leaking that env var, e.g. via a secrets manager rather than a
      committed CI file).
    - Otherwise prompted for interactively (input hidden, confirmed twice)
      via getpass, and never printed or logged back out.
    - Ignored entirely when promoting an existing user -- there's nothing
      to set it on.

Safety:
    - Requires --yes (or an interactive "yes" confirmation) before writing
      anything, so it can't be triggered by an accidental copy-paste.
    - Idempotent: running it again against the same email is always safe
      and just reports the current state.
"""

import argparse
import asyncio
import getpass
import sys

from pydantic import ValidationError

from app.core.database import async_session_factory
from app.modules.auth.exceptions.auth_exceptions import (
    EmailAlreadyExistsError,
    UsernameAlreadyExistsError,
)
from app.modules.auth.repositories.auth_repository import AuthRepository
from app.modules.auth.schemas.auth import RegisterRequest
from app.modules.auth.services.auth_service import AuthService
from app.modules.users.repositories.user_repository import UserRepository
from app.shared.enums.roles import UserRole


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or promote the first admin user for this deployment."
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Email of the account to create (if new) or promote (if it already exists).",
    )
    parser.add_argument(
        "--username",
        default="admin",
        help="Username to use ONLY when creating a brand-new account (default: admin). "
        "Ignored when the email already belongs to an existing user.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the interactive confirmation prompt. Required for non-interactive use.",
    )
    return parser.parse_args()


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "Refusing to proceed without --yes: stdin is not a terminal, "
            "so an interactive confirmation can't be collected.",
            file=sys.stderr,
        )
        return False
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def _read_password() -> str:
    import os

    env_password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD")
    if env_password:
        return env_password

    while True:
        password = getpass.getpass("New admin password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords did not match -- try again.\n", file=sys.stderr)
            continue
        return password


async def main() -> None:
    args = _parse_args()

    async with async_session_factory() as db:
        auth_repo = AuthRepository(db)
        user_repo = UserRepository(db)
        auth_service = AuthService(auth_repo)

        existing = await auth_service.get_user_by_email(args.email)

        if existing is not None:
            if existing.role == UserRole.ADMIN:
                print(f"'{args.email}' is already an admin. Nothing to do.")
                return

            if not _confirm(
                f"Promote existing user '{args.email}' (role={existing.role.value}) to ADMIN?",
                assume_yes=args.yes,
            ):
                print("Aborted.")
                return

            await user_repo.update(existing, role=UserRole.ADMIN)
            await db.commit()
            print(f"Promoted '{args.email}' to ADMIN.")
            return

        # No account with this email exists yet -- create one, then promote it.
        password = _read_password()

        try:
            RegisterRequest(username=args.username, email=args.email, password=password)
        except ValidationError as exc:
            print("Invalid input:", file=sys.stderr)
            for error in exc.errors():
                field = ".".join(str(loc) for loc in error["loc"])
                print(f"  - {field}: {error['msg']}", file=sys.stderr)
            sys.exit(1)

        if not _confirm(
            f"Create new admin account '{args.username}' <{args.email}>?",
            assume_yes=args.yes,
        ):
            print("Aborted.")
            return

        try:
            user = await auth_service.register(
                username=args.username, email=args.email, password=password
            )
        except (EmailAlreadyExistsError, UsernameAlreadyExistsError) as exc:
            # Narrow race: something else created a colliding user between
            # our lookup above and this call.
            print(f"Could not create user: {exc}", file=sys.stderr)
            sys.exit(1)

        await user_repo.update(user, role=UserRole.ADMIN)
        await db.commit()
        print(f"Created '{args.email}' and promoted it to ADMIN.")


if __name__ == "__main__":
    asyncio.run(main())
