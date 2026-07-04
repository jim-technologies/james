# Proto toolchain. Run via the repo's flox env, e.g. `flox activate -- make -C api generate`.
# buf.lock is committed; deps are fetched from the BSR per the lock (and cached),
# so `generate` is deterministic and is verified in CI by the root `gen-check`.

generate:
	rm -rf ../gen/python          # prune orphans so deletions/renames show as drift
	cd proto && buf generate
	cd proto && buf build -o ../../gen/descriptor.binpb

format:
	cd proto && buf format -w

format-check:
	cd proto && buf format --diff --exit-code

lint:
	cd proto && buf lint

# Refresh the pinned BSR dependencies (updates buf.lock). Run manually, not in CI.
deps:
	cd proto && buf dep update

.PHONY: generate format format-check lint deps
