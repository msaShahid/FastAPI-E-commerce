# CLAUDE.md

Project instructions for Claude. Put this file in the root of **both** repositories: the NovaCart Next.js frontend and the FastAPI backend. Sections marked (Frontend) or (Backend) apply to one repo only.

## Read first

1. Read this file, then `CLAUDE_HANDOVER.md` (architecture, status, decisions, open issues), before making any change.
2. The handover is a snapshot. Check it against the code (`git log`, the files it names, `ACCEPTANCE.md` in the frontend, `docs/STATUS.md` in the backend). If anything disagrees, trust the code and the owner's latest message, and say so.
3. Do not invent missing information. If you do not know something, say so and ask.

## How to work with the owner

- Be concise. Keep planning short, implement in small phases, and do not repeat summaries or explanations.
- **Resource-conscious:** run focused tests while developing; run the full suite once, at a milestone gate. Do not run builds, scripts or analysis that do not validate a change.
- **Never claim anything unverified.** Label evidence VERIFIED, UNVERIFIED or NOT RUN, and say where it was run (sandbox, real backend, browser, the owner's machine).
- Milestones M0 to M7 are each reviewed and approved by the owner before the next starts. Do not start the next milestone without an explicit go-ahead ("move next" has been used as one).
- No standalone HTML prototypes for sign-in, registration and account pages (owner's instruction). Follow the existing approved design system, folder structure and conventions; do not introduce new architectural patterns or duplicate implementations.
- Completion report format: what changed; files (and migration IDs); exact commands and tests actually run; pass or fail; blockers; decisions needed from the owner.
- PowerShell commands go one per line (the owner uses Windows, PowerShell and Docker).
- Never use the words "seller" or "marketplace". Use "business GST registration" and "business tax configuration".
- The GST registration count is undecided: ask before building anything that depends on it.

## Backend changes (owner-authorised)

The frontend work may require backend changes. Make them in the backend repo following its structure and conventions, keep them additive and compatible, and deliver **a ZIP of only the changed and new files in their exact folder structure** so the owner can extract it over the project. State which tests you ran and which you did not.

## Money and data rules (both repos)

- Money is integer minor units from the backend. **The frontend never computes prices, tax, shipping, discounts or totals**; it shows backend values.
- Never put secrets, tokens, real card numbers or personal data in logs, responses to the browser, test fixtures or this repo. The demo gateway uses fake test details only.
- Tokens never reach the browser: they live only in sealed HttpOnly cookies set by the BFF.

## (Frontend) Rules enforced by tooling

- Same-origin BFF: the browser calls only `/api/bff/*`. Every mutating route uses `mutatingRoute` or `handleBff` (`src/lib/bff/`): strict Origin check plus the `X-NC-Csrf` header; password change, email change and address delete also need the `X-NC-Sensitive` page token. Validate request bodies with zod before calling the backend, and never accept a price from the browser.
- Layering (ESLint boundaries and dependency-cruiser): shared code does not import `app` or `features`; features import each other only through `index.tsx` (client-safe) or `server.ts` (server-only); wire types only in `lib/api` and feature `services/`.
- No `process.env` or `NEXT_PUBLIC_*` outside `src/config/env.server.ts` (build once, runtime env). No arithmetic on money outside `src/lib/money`. No literal user-visible text in components: use `messages/en.json` (English only). Error text comes from message keys, never from raw backend wording.
- Security-critical tests are listed in `tests/protected-tests.json`. Do not rename or delete them without updating that file.
- Generated API types (`src/generated/`) are not committed. After changing `spec/openapi.json` run `pnpm gen:api` (`pnpm typecheck` and `pnpm build` do it). Refresh the snapshot with `pnpm spec:sync <openapi.json>`.
- Edit `messages/en.json`, `package.json` and similar files without reformatting unrelated lines; run `pnpm format` (Prettier) before reporting.

## (Backend) Conventions

- Module layout: `app/modules/<module>/{api,models,repositories,schemas,services,dependencies,exceptions}`; shared pieces in `app/core`.
- Errors: exception classes carry a class attribute `code` (and optionally `extra`); the client branches on `code` and status, never on message wording. After any API or error-code change run `python -m scripts.export_api_docs` (regenerates `docs/openapi.json` and `docs/ERROR_CODES.md`; drift tests check them).
- Migrations: Alembic, additive. Models are imported by hand in `alembic/env.py`, so a new model must be added there (a test checks this). Verify with `alembic upgrade head` and `alembic check`.
- The project mixes CRLF and LF line endings: preserve each file's own endings when editing.
- Register new routers in `app/main.py`. Settings live in `app/core/config.py` and every name in `.env.example` and `.env.production.example` must be a real setting.
- Do not weaken a security or concurrency test to make it pass. If a deliberate behaviour change breaks one, change the test to pin the old rule explicitly (as was done for the refresh-token strict mode) and add a test for the new rule.

## Commands

Frontend (PowerShell): `pnpm install`, `pnpm dev`, `pnpm test:focus <path>`, `pnpm test:full` (gate: spec check, tests, lint, typecheck, cycles, protected tests), `pnpm build`, `pnpm e2e tests/e2e/<spec>.ts` (one worker by default; prerequisites, including relaxed per-address backend rate limits for a test stack, are in `tests/e2e/README.md`).

Backend (Docker): `docker compose up -d --build`, `docker compose exec api alembic upgrade head`, `docker compose exec api alembic check`, `docker compose exec api pytest <path> -q`, `docker compose exec api python -m scripts.export_api_docs`. The full backend suite takes about 15 minutes: run it only at a milestone gate.

## Before you finish any task

- Re-read what you changed. State what you verified and how, and what you did not.
- Update `ACCEPTANCE.md` (frontend) or `docs/` (backend) when a status changes, and add anything new that a future session needs to `CLAUDE_HANDOVER.md`.
