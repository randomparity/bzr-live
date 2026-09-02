.PHONY: init up down doctor reset clean checkpoint-test checkpoint-smoke replay-smoke test check \
	build-multiarch

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

checkpoint-test:
	@uv run --python 3.11 python -m unittest tests.test_checkpoint -v

checkpoint-smoke:
	@bash tests/checkpoint_smoke.sh

replay-smoke:
	@bash tests/replay_smoke.sh

test:
	@bash tests/lifecycle_test.sh
	@uv run --python 3.11 python -m unittest discover -s tests -v

check:
	@bash -n \
		scripts/lifecycle \
		containers/bugzilla/entrypoint.sh \
		tests/lifecycle_test.sh \
		tests/checkpoint_smoke.sh \
		tests/provision_smoke.sh \
		tests/replay_smoke.sh
	@shellcheck \
		scripts/lifecycle \
		containers/bugzilla/entrypoint.sh \
		tests/lifecycle_test.sh \
		tests/checkpoint_smoke.sh \
		tests/provision_smoke.sh \
		tests/replay_smoke.sh
	@uv run --python 3.11 python -m compileall -q src tests
	@BZ_ADMIN_PASSWORD=check BZ_DB_PASSWORD=check MARIADB_ROOT_PASSWORD=check \
		docker compose --file compose.yaml config --quiet

build-multiarch:
	@docker buildx build \
		--platform linux/amd64,linux/arm64 \
		--file containers/bugzilla/Dockerfile \
		--output type=cacheonly \
		.
