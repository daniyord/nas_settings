# The stack runs on the NAS's own docker daemon, reached over ssh (Host nas / nas-lan in linux_settings' settings/ssh/config).
# The tailscale container uses host networking, so recreating or stopping it cuts an ssh session that goes over the
# tailnet; use the LAN route whenever it is reachable.
NAS_LAN_IP := 192.168.0.148
ON_LAN := $(shell timeout 2 bash -c '</dev/tcp/$(NAS_LAN_IP)/22' 2>/dev/null && echo yes)
export DOCKER_HOST := ssh://$(if $(ON_LAN),nas-lan,nas)
COMPOSE := docker compose --project-directory docker

.PHONY: up hard down

up:
	$(COMPOSE) build
	$(COMPOSE) up -d --remove-orphans

hard: up
	$(COMPOSE) pull
	docker image prune -f
	$(COMPOSE) restart

# Over the tailnet this would take the NAS off the tailnet mid-command, leaving it unreachable until back on the LAN
down:
	@[ -n "$(ON_LAN)" ] || { echo "make down needs the NAS LAN ($(NAS_LAN_IP)); refusing over the tailnet" >&2; exit 1; }
	$(COMPOSE) down
