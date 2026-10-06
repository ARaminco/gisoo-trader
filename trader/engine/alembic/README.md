# Schema migrations

`versions/` is empty on purpose. Until it has a revision **and** `alembic` is in
`requirements.txt`, the `migrate` service (`python -m app.db.migrate`) falls back
to `Base.metadata.create_all`, which only *adds* missing tables and never alters
an existing column.

Before the first change to an existing model, adopt Alembic:

1. Add `alembic` to `trader/engine/requirements.txt` and rebuild
   (`docker compose build migrate`). `alembic.ini` and `alembic/env.py` are
   already in place and use the app's `DATABASE_URL`.
2. Generate the baseline **against an empty database** (autogenerate against
   the live one would produce an empty revision, because the tables exist):

   ```bash
   docker compose exec postgres createdb -U trader scratch
   docker compose run --rm --no-deps \
     -e DATABASE_URL=postgresql+asyncpg://trader:<POSTGRES_PASSWORD>@postgres:5432/scratch \
     migrate alembic revision --autogenerate -m baseline
   docker compose exec postgres dropdb -U trader scratch
   ```

   The file lands in `trader/engine/alembic/versions/` (bind-mounted into the
   `migrate` service). Review it and commit it.
3. `docker compose up -d`. A database that has the tables but no
   `alembic_version` is stamped at the baseline and upgraded to head. Set
   `MIGRATE_AUTO_STAMP=false` in `.env` to refuse that and stamp by hand.

After that, for every model change:
`docker compose run --rm --no-deps migrate alembic revision --autogenerate -m "add x"`,
review, commit, and `./start.sh rebuild` applies it before any worker starts.
Rollback: `docker compose run --rm --no-deps migrate alembic downgrade -1`
(after stopping the workers), or restore a dump from `./backups`.
`python -m app.db.migrate --check` prints which path will be taken.

Nothing in this directory is imported by the workers; it is used only by the
`alembic` CLI and by `app/db/migrate.py`.
