# Boardgames on the NAS

NAS side of the board game platform in `/shared/projects/boardgames` (plan: `docs/games/feudal-kingdom/PLAN.md` §9a there). Everything for it is kept apart from the other services:

| What | Where |
|------|-------|
| Compose services | `docker/boardgames/docker-compose.yml`, pulled into the `nas` stack by `include:` in `docker/docker-compose.yml`, so `make up` covers it |
| Secrets | `docker/boardgames/.env` (gitignored): `BOARDGAMES_DEV_DB_PASSWORD`, `BOARDGAMES_PROD_DB_PASSWORD` |
| Data | `/volume1/ServerConfigurations/Boardgames/<env>/` |

## Environments

| Env | Where the app runs | Database |
|-----|--------------------|----------|
| dev | On whichever PC is being used (`make dev` in the boardgames repo) | `boardgames-dev-db` on the NAS, port 5433 |
| prod | On the NAS: `boardgames`, `http://nas:8110` (LAN or tailnet) | `boardgames-db`, no published port |

## dev database

- `boardgames-dev-db`: Postgres 17, `nas:5433` (LAN or tailnet), database `boardgames_dev`, user `boardgames`, password `BOARDGAMES_DEV_DB_PASSWORD`. Data in `/volume1/ServerConfigurations/Boardgames/dev/postgres`.
- On each PC, the boardgames repo's `apps/api/.env` (gitignored) holds the connection string; copy it from `apps/api/.env.example` and fill in the password.
- The real Kingdom Legacy campaign is played against this database during development, so treat it as data worth keeping, not a scratch database.

```bash
# Start or recreate only the boardgames services
DOCKER_HOST=ssh://nas docker compose --project-directory docker up -d --no-deps boardgames-dev-db

# psql
DOCKER_HOST=ssh://nas docker exec -it boardgames-dev-db psql -U boardgames boardgames_dev
```

## prod

- `boardgames`: the app image, built on the NAS from the boardgames repo (`build.context` is `/shared/projects/boardgames` on the PC, sent over ssh). Port 8110. Its entrypoint runs the Alembic migrations, then uvicorn.
- `boardgames-db`: Postgres 17, database `boardgames_prod`, reachable only by the app. Data in `/volume1/ServerConfigurations/Boardgames/prod/postgres`.
- Deploy from the boardgames repo, not with this repo's `make up` (which also works, but takes no backup):
  ```bash
  cd /shared/projects/boardgames
  make deploy-prod            # backup of boardgames_prod, then build + up -d boardgames
  make logs                   # follow the prod app's log
  make psql ENV=prod          # or ENV=dev
  ```

## Backups

`make backup ENV=dev|prod` in the boardgames repo writes a gzipped `pg_dump` to `/volume1/ServerConfigurations/Boardgames/<env>/backups` (mounted as `/backups` in each database container). `make deploy-prod` takes one before every deploy. Restore:

```bash
gunzip -c /volume1/ServerConfigurations/Boardgames/prod/backups/<file>.sql.gz \
  | docker exec -i boardgames-db psql -U boardgames boardgames_prod   # on the NAS, into an empty database
```

