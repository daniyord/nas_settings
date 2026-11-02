# NAS Docker stack

Services running on the UGREEN NAS (`192.168.0.148`, tailnet `nas`), driven from a PC over SSH (`Host nas` from `/shared/linux_settings/settings/ssh/config`). SSH access, the NAS shell and the PC-side SMB mounts are covered by `/shared/linux_settings/docs/SETUP_nas.md`.

## Docker stack

The compose stack lives in `docker/`. It runs on the NAS's own Docker daemon and is driven from the PC over SSH; the root `Makefile` sets `DOCKER_HOST=ssh://nas`:

```bash
make up     # build + up -d --remove-orphans (default target)
make hard   # also pull, prune images, restart
make down

# Single service:
cd docker && DOCKER_HOST=ssh://nas docker compose up -d calibre-web
```

Secrets (`TS_AUTHKEY`, `JELLYFIN_API_KEY`) go in the gitignored `docker/.env`. App data lives in `/volume1/ServerConfigurations/<App>`.

| Service | Port | Notes |
|---------|------|-------|
| `jellyfin` | 8096 | Media from `/volume1/Movies1`, `/volume2/Movies2`, `/volume3/Movies3` |
| `calibre-web` | 8083 | Library at `/volume1/Common/Other/Books/Calibre`; includes `calibredb` via `DOCKER_MODS`; built from `docker/calibre-web` to add the `calibre-ingest` service |
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
- Ingest folders, checked every minute by the `calibre-ingest` service (`docker/calibre-web/calibre-ingest`); each file is added, deleted, and Calibre-Web restarted. Files that fail to import go to a `failed` subfolder. Logs: `docker logs calibre-web`.
  - `Common/Other/Books/Ingest` — normal books; title and author come from the book's own metadata.
  - `Common/Other/Books/Ingest-Boardgames` — board game rulebooks; author "Настолни игри", tag "настолна игра", filename as title.
- Back up `metadata.db` before bulk edits, and don't edit the library from Calibre desktop and Calibre-Web at the same time.
- In the **Books list** table view, sorting by the *Authors* column shows multi-author books once per author. This is Calibre-Web behavior; the library isn't duplicated.

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
