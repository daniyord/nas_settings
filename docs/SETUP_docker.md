# NAS Docker stack

Services running on the UGREEN NAS (`192.168.0.148`, tailnet `nas`), driven from a PC over SSH (`Host nas` from `/shared/linux_settings/settings/ssh/config`). SSH access, the NAS shell and the PC-side SMB mounts are covered by `/shared/linux_settings/docs/SETUP_nas.md`.

## Docker stack

The compose stack lives in `docker/`. It runs on the NAS's own Docker daemon and is driven from the PC over SSH; the root `Makefile` sets `DOCKER_HOST=ssh://nas`:

```bash
make up     # build + up -d --remove-orphans (default target)
make hard   # also pull, prune images, restart
make down

# Single service:
DOCKER_HOST=ssh://nas docker compose --project-directory docker up -d calibre-web
```

Services are grouped one folder per purpose. `docker/docker-compose.yml` holds only `tailscale` and pulls the groups in with `include:`, so everything is still one `nas` project on one network (`jellyfin-tpdb-match` reaches `http://jellyfin:8096`) and `make up` covers it all:

| Folder | Services | Secrets (gitignored) |
|--------|----------|----------------------|
| `docker/` | `tailscale` | `docker/.env`: `TS_AUTHKEY` |
| `docker/media/` | `jellyfin`, `jellyfin-tpdb-match`, `jellyfin-movie-ingest` (built from their folders in `media/`) | `docker/media/.env`: `JELLYFIN_API_KEY`, `TMDB_API_KEY` |
| `docker/books/` | `calibre-web` (built from `books/calibre-web`) | — |
| `docker/boardgames/` | `boardgames`, `boardgames-db`, `boardgames-dev-db` | `docker/boardgames/.env`, see [SETUP_boardgames.md](SETUP_boardgames.md) |
| `docker/ci/` | `github-runner`, `github-runner-docker` | `docker/ci/.env`: `GITHUB_RUNNER_ACCESS_TOKEN` |

A new group gets its own folder with a `docker-compose.yml` (and `.env` if it needs secrets) and a line in the `include:` list. App data lives in `/volume1/ServerConfigurations/<App>`.

| Service | Port | Notes |
|---------|------|-------|
| `jellyfin` | 8096 | Media from `/volume1/Movies1`, `/volume2/Movies2`, `/volume3/Movies3` |
| `jellyfin-tpdb-match` | 8097 | ThePornDB matcher with a status page for the adult library |
| `jellyfin-movie-ingest` | 8098 | Renames new movies in the film libraries after Jellyfin's match (`Title (Year) [imdbid-…]`) and organizes series release folders (`S01` in the name) into `Title (Year) [imdbid-…]/Season NN/SxxEyy - Title`, sets their Bulgarian sort name, checks subtitle sync with `ffsubsync`; status page to confirm doubtful matches and fix subtitles |
| `calibre-web` | 8083 | Library at `/volume1/Common/Other/Books/Calibre`; includes `calibredb` via `DOCKER_MODS`; built from `docker/books/calibre-web` to add the `calibre-ingest` service |
| `boardgames` | 8110 | Board game platform (prod), with its database `boardgames-db`; see [SETUP_boardgames.md](SETUP_boardgames.md) |
| `boardgames-dev-db` | 5433 | Board game platform dev database, used by the app running on the PCs |
| `github-runner` | — | Self-hosted GitHub Actions runner for `daniyord/boardgames`, see [GitHub Actions runner](#github-actions-runner) |
| `tailscale` | — | Remote access, see [Tailscale](#tailscale-one-time) below and `/shared/linux_settings/docs/SETUP_tailscale.md` for the PCs |

qBittorrent is a UGOS App Center app and stays managed by UGOS.

## Calibre-Web

- First login is `admin` / `admin123` — change it right away. The database location is `/books`.
- Bulk-add books with `calibredb` inside the container, as the `abc` user so file ownership stays right:
  ```bash
  DOCKER_HOST=ssh://nas docker exec -u abc -e HOME=/config calibre-web \
    calibredb add --with-library /books --authors "..." --tags книга-игра --languages bul /path/in/container.pdf
  ```
  Restart the container afterwards (`docker restart calibre-web`) so Calibre-Web picks up the changes.
- Ingest folders, checked every minute by the `calibre-ingest` service (`docker/books/calibre-web/calibre-ingest`); each file is added, deleted, and Calibre-Web restarted. Files that fail to import go to a `failed` subfolder. Logs: `docker logs calibre-web`.
  - `Common/Other/Books/Ingest` — normal books; title and author come from the book's own metadata.
  - `Common/Other/Books/Ingest-Boardgames` — board game rulebooks; author "Настолни игри", tag "настолна игра", filename as title.
- Back up `metadata.db` before bulk edits, and don't edit the library from Calibre desktop and Calibre-Web at the same time.
- In the **Books list** table view, sorting by the *Authors* column shows multi-author books once per author. This is Calibre-Web behavior; the library isn't duplicated.

## GitHub Actions runner

`github-runner` (image `myoung34/github-runner`) is a self-hosted runner for the private repo `daniyord/boardgames`, labels `self-hosted`, `linux`, `x64`, `nas` (workflows use `runs-on: [self-hosted, nas]`). Only one job runs at a time.

- **Ephemeral**: the runner registers, takes one job and exits; `restart: unless-stopped` starts it again and it registers anew as `nas-<random>`. Restarting keeps the container's filesystem, so packages a job installs with `sudo apt-get` stay until the container is recreated.
- **Docker**: jobs' `services:` containers run on `github-runner-docker`, a private Docker-in-Docker daemon (privileged, unix socket only, data in `/volume1/ServerConfigurations/GitHubRunner/docker`), not on the NAS's daemon. The runner shares its network namespace, so a service with `ports: 5432:5432` is reachable from the steps at `localhost:5432`. The NAS host's own `127.0.0.1:5432` is UGOS's Postgres, which is why the runner is not on host networking. Both are on their own `ci` network, apart from the other services.
- **Limits**: runner 3 GB / 4 CPUs; the dind daemon and its service containers 1 GB / 2 CPUs.
- **Data**: `/volume1/ServerConfigurations/GitHubRunner/{toolcache,work,docker}`; the tool cache keeps `setup-node` / `setup-uv` downloads across jobs.
- **Token**: `GITHUB_RUNNER_ACCESS_TOKEN` in `docker/ci/.env` is a fine-grained PAT limited to `daniyord/boardgames` with the repository permission **Administration: Read and write**. The runner uses it to fetch a registration token on every start. When the PAT expires, the runner can no longer register: put a new one in `.env` and recreate the runner (`up -d github-runner`).
- **Security**: never point it at a public repo, because PRs from forks would run their code on the NAS.

```bash
DOCKER_HOST=ssh://nas docker compose --project-directory docker up -d github-runner
DOCKER_HOST=ssh://nas docker logs -f github-runner
```

Clean slate for the runner (drops anything apt-installed; the tool cache and the dind image cache are bind mounts and stay): `up -d --force-recreate github-runner`.

## Tailscale (one-time)

The NAS runs Tailscale as the `tailscale` service in `docker/docker-compose.yml` (host networking, state in `/volume1/ServerConfigurations/Tailscale`).

1. In the Tailscale admin console: **Settings → Keys → Generate auth key** (one-off is enough).
2. Put it in `docker/.env` (gitignored):
   ```
   TS_AUTHKEY=tskey-auth-...
   ```
3. Start it:
   ```bash
   cd /shared/nas_settings/docker
   DOCKER_HOST=ssh://nas docker compose up -d tailscale
   DOCKER_HOST=ssh://nas docker exec tailscale tailscale status
   ```
4. In the admin console: **Machines → nas → … → Disable key expiry**, otherwise the NAS drops off the tailnet after 180 days.
5. Optionally delete the key from `docker/.env` — the login is kept in the state directory.
