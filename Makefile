.PHONY: init up down doctor reset clean test check build-multiarch

init:
	@./scripts/lifecycle init

up:
	@./scripts/lifecycle up

down:
	@./scripts/lifecycle down

doctor:
	@./scripts/lifecycle doctor

reset:
	@./scripts/lifecycle reset

clean:
	@./scripts/lifecycle clean

test:
	@bash tests/lifecycle_test.sh

check:
	@bash -n scripts/lifecycle containers/bugzilla/entrypoint.sh tests/lifecycle_test.sh
	@shellcheck scripts/lifecycle containers/bugzilla/entrypoint.sh tests/lifecycle_test.sh
	@BZ_ADMIN_PASSWORD=check BZ_DB_PASSWORD=check MARIADB_ROOT_PASSWORD=check \
		docker compose --file compose.yaml config --quiet

build-multiarch:
	@docker buildx build \
		--platform linux/amd64,linux/arm64 \
		--file containers/bugzilla/Dockerfile \
		--output type=cacheonly \
		.
