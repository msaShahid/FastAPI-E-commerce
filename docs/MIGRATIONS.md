# Database migrations: the contract

**Production recovery is a restore from backup, never `alembic downgrade`.**

## What is guaranteed and tested

| | Guaranteed | Checked by |
|---|---|---|
| `alembic upgrade head` on an empty database | yes | CI `migrations` job, `tests/integration/test_migrations.py` |
| Upgrade of an older database that holds rows (legacy orders keep their data and are never given a guessed currency) | yes | `test_upgrading_an_older_database_with_legacy_rows...` |
| A single head; models and migrated schema agree (`alembic check`) | yes | CI, `test_a_fresh_database_upgrades...` |
| Triggers, constraints, indexes, function bodies, enums and columns of a migrated database equal those of the `create_all` schema the test suite uses | yes | `test_the_migrated_schema_is_the_schema_the_test_suite_runs_against` |
| `alembic downgrade base` on an **empty** database, then `upgrade head` again | yes | CI, `test_a_full_downgrade_leaves_nothing_behind...` |
| Downgrade of a database **with data** | **no** | see below |

## Downgrades and data

Several migrations drop columns or tables, or restore `NOT NULL` / unique constraints that real data can violate (for example,
guest orders have no user, so restoring `orders.user_id NOT NULL` fails). So `alembic/env.py` **refuses** a downgrade when
`users`, `products`, `orders` or `payments` hold rows, changes nothing (the run is one transaction), and says why.

For a throwaway database only:

```
alembic -x allow_data_loss=true downgrade <revision>
```

That override can still fail partway on data it cannot reverse; on a failure nothing is changed.

Left behind by a full downgrade, deliberately: the `btree_gist` and `pg_trgm` extensions (created with `IF NOT EXISTS`, possibly
shared with other databases). Re-upgrading is unaffected.

## Writing a migration

* Every `upgrade()` needs a `downgrade()` that undoes it on an empty database. A table that creates a **native enum** must drop
  the type in its downgrade (`DROP TYPE IF EXISTS`).
* Do not edit a migration that has been released; add a new one. (Only `downgrade()` bodies of the four table-creating
  migrations were corrected to drop their enum types.)
* Keep guard functions (`CREATE OR REPLACE FUNCTION`) in sync with the model copy (`app/modules/*/models/guard.py`); tests compare them.
* Prefer additive, nullable changes, so a deploy can be rolled back by redeploying the previous code against the new schema.

## Deploying

Run migrations as a separate, deliberate step before starting the new version, after taking a backup:

```
docker compose -f docker-compose.prod.yml --profile migrate run --rm migrate
```

## Product variants (b4d8e2a6c915, c7f1a9d3e526)

Two additive revisions: the first is transactional with a 3-second lock timeout and refuses PostgreSQL older than 15 or colliding SKUs; the second builds indexes concurrently, back-fills in batches and validates constraints, and is safe to run again. Sequence, pre-flight queries, recovery and the application-role recipe are in `docs/VARIANTS.md`.
