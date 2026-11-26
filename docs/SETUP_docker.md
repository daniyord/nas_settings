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

Services are grouped one folder per purpose. `docker/docker-compose.yml` holds only `tailscale` and pulls the groups in with `include:`, so everything is still one `nas` project on one network (`media-ingest` reaches `http://jellyfin:8096`) and `make up` covers it all:

| Folder | Services | Secrets (gitignored) |
|--------|----------|----------------------|
| `docker/` | `tailscale` | `docker/.env`: `TS_AUTHKEY` |
| `docker/media/` | `jellyfin`, `media-ingest`, `media-subtitles`, `media-adult`, `media-auth`, `media-web` (built from their folders in `media/`, the `media-*` services with the shared `media/common/`), `qbittorrent`, `prowlarr` | `docker/media/.env`: `JELLYFIN_API_KEY`, `TMDB_API_KEY`, `TPDB_API_TOKEN` (ThePornDB, for `media-adult`) |
| `docker/books/` | `calibre-web` (built from `books/calibre-web`) | — |
| `docker/boardgames/` | `boardgames`, `boardgames-db`, `boardgames-dev-db` | `docker/boardgames/.env`, see [SETUP_boardgames.md](SETUP_boardgames.md) |
| `docker/ci/` | `github-runner`, `github-runner-docker` | `docker/ci/.env`: `GITHUB_RUNNER_ACCESS_TOKEN` |

A new group gets its own folder with a `docker-compose.yml` (and `.env` if it needs secrets) and a line in the `include:` list. App data lives in `/volume1/ServerConfigurations/<App>`.

| Service | Port | Notes |
|---------|------|-------|
| `jellyfin` | 8096 | Media from `/volume1/Movies1`, `/volume2/Movies2`, `/volume3/Movies3` |
| `media-ingest` | (8098, through `media-web`) | Renames new movies in the film libraries after Jellyfin's match (`Title (Year) [imdbid-…]`) and organizes series release folders (`S01` in the name) into `Title (Year) [imdbid-…]/Season NN/SxxEyy - Title`, sets their Bulgarian sort name; status page to confirm doubtful matches. Moves finished qBittorrent downloads in a category named after a film or series library into it. Data in `/volume1/ServerConfigurations/MediaIngest` |
| `media-subtitles` | (8099, through `media-web`) | Checks subtitle sync with `ffsubsync` and fixes a shift of up to 20 s (alone or with a 23.976/24/25 fps conversion) by itself, keeping the original as `.orig`; downloads Bulgarian subtitles from subs.sab.bz and subsunacs.net for every organized movie and episode with sound other than Bulgarian (a second track next to a Bulgarian one counts) and no good Bulgarian subtitle (not in the Bulgarian libraries), keeping only one that lines up with the speech and setting a badly off one aside as `.replaced`; a movie or series whose one sound track names no language waits on the status page for an answer, Bulgarian dub (its track is then set to `bul`: in place in an MKV with `mkvpropedit`, an AVI/MP4/MPG is rewritten as MKV with `mkvmerge` without encoding, keeping the name) or subtitles; status page to fix or restore subtitles and follow the downloads. Data in `/volume1/ServerConfigurations/MediaSubtitles` |
| `media-adult` | (8100, through `media-web`) | The adult libraries „Порно“ and „Порно - сцени“ (docs/PLAN_media_services.md, stage 4). Finished torrents in the qBittorrent category `Порно` (pointed at `Downloads` on whichever adult disk has more free space, `/volume5/MoviesAdult/Downloads` or `/volume6/MoviesAdult1/Downloads`) are matched in ThePornDB and moved into `Porn/<Studio>/<Title (Year)> [tpdbid-…]/` on the same disk with our NFO and artwork; doubtful ones wait on its status page. A download inside a folder named after a studio of the library (`Downloads/<Studio>/…`, or the qBittorrent subcategory `Порно/<Studio>`, whose path it keeps in step) is searched only in that studio's ThePornDB sites and placed under it on its own when found; a scene in `Downloads/Сцени/<Studio>/` goes straight under that studio. Scenes: the category `Порно - сцени` downloads into `Downloads/Сцени` and waits on the page for its studio, then goes to `PornScenes/<Studio>/` with an NFO and a poster with its title; a scene copied by hand into a studio folder gets them on the next pass. `/adult/performers` is the performers' catalog (from the NFOs, with their ThePornDB or TMDB photos and titles); the status page's „Изпълнители“ card lists those ThePornDB has no photo of and the movies with none. Data in `/volume1/ServerConfigurations/MediaAdult` |
| `media-auth` | (8110, through `media-web`) | Who may open the status pages: sign-in at `/auth/login` with a Jellyfin user (checked by Jellyfin, whose access token is kept in a cookie for 30 days; a changed password or a signed-out device in Jellyfin ends it). `media-web`'s nginx asks it before every request (`auth_request`): Jellyfin's administrators see and change everything, a user who sees the library „Порно“ only looks at `/adult/` (GET), anyone else nothing |
| `media-web` | 8090 | The only address of the status pages (the services publish no port): `/ingest/` is `media-ingest`'s, `/subtitles/` is `media-subtitles`'s, `/adult/` is `media-adult`'s (nginx in `media-web/`); their top bar links the ones the signed-in user may see; every request is checked with `media-auth` first |
| `qbittorrent` | 8888 | Host network; a `Downloads` folder on every volume with a film library (`/volume1/Downloads`, `/volume{2,3}/Movies{2,3}/Downloads`) and on both adult disks (for `media-adult`). Each library's category is pointed by `media-ingest` at the one with the most free space, which new torrents follow because Automatic Torrent Management is the default (`Session\DisableAutoTMMByDefault=false`; without it they all land in `/downloads`); no WebUI login from Docker's networks |
| `prowlarr` | 9696 | Tracker search; grabs go to `qbittorrent` with category `Филми` (Movies) or `Сериали` (TV), set in its download client's category mappings; anything else goes without a category to `/downloads` and is left alone |
| `calibre-web` | 8083 | Library at `/volume1/Common/Other/Books/Calibre`; includes `calibredb` via `DOCKER_MODS`; built from `docker/books/calibre-web` to add the `calibre-ingest` service |
| `boardgames` | 8110 | Board game platform (prod), with its database `boardgames-db`; see [SETUP_boardgames.md](SETUP_boardgames.md) |
| `boardgames-dev-db` | 5433 | Board game platform dev database, used by the app running on the PCs |
| `github-runner` | — | Self-hosted GitHub Actions runner for `daniyord/boardgames`, see [GitHub Actions runner](#github-actions-runner) |
| `tailscale` | — | Remote access, see [Tailscale](#tailscale-one-time) below and `/shared/linux_settings/docs/SETUP_tailscale.md` for the PCs |

To download a film or a series, give the torrent in qBittorrent the category named after its library (`Филми`, `Филми - детски`, `Филми - български`, `Филми - руски`, `Сериали`, `Сериали - детски`, `Сериали - български`, `Сериали - руски`); Prowlarr already sets `Филми` or `Сериали`, so only the other ones are changed by hand while it downloads. Once it finishes, `media-ingest` moves it into that library on the same disk (a rename) and drops the torrent, so nothing is seeded. Every torrent gets its own folder (Create subfolder), so a single episode becomes a release folder that is organized into its series; an episode of a series already in the library is moved, while it downloads, to the download folder on that series' disk.

## Checks

The services' Python, the Dockerfiles, the compose files and `calibre-ingest` are checked on every commit by a pre-commit hook (`.pre-commit-config.yaml`: `ruff`, `basedpyright`, `shellcheck`, `hadolint`, `docker compose config`); the tools come from `pyproject.toml`/`uv.lock` and run locally, nothing is installed on the NAS. Once per clone:

```bash
uv sync
uv run pre-commit install
```

`make lint` runs the same checks over every file, `make test` the tests (`docker/*/*/tests/`). Findings in the code from before the checks are listed in `.basedpyright/baseline.json`, so only new ones are reported.

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
