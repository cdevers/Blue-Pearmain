VERSION := $(shell grep '^version' pyproject.toml | sed 's/.*= *"\(.*\)"/\1/')
ARCHIVE := blue-pearmain-$(VERSION).tar.gz

.PHONY: dist test lint clean install-hooks bump changelog

# Bump version, commit, and tag. Usage: make bump [part=minor|major]  (default: patch)
bump:
	@CURRENT=$(VERSION); \
	MAJOR=$$(echo $$CURRENT | cut -d. -f1); \
	MINOR=$$(echo $$CURRENT | cut -d. -f2); \
	PATCH=$$(echo $$CURRENT | cut -d. -f3); \
	if [ "$(part)" = "major" ]; then NEW="$$((MAJOR+1)).0.0"; \
	elif [ "$(part)" = "minor" ]; then NEW="$$MAJOR.$$((MINOR+1)).0"; \
	else NEW="$$MAJOR.$$MINOR.$$((PATCH+1))"; fi; \
	sed -i '' "s/^version = \".*\"/version = \"$$NEW\"/" pyproject.toml; \
	git add pyproject.toml; \
	git commit -m "Bump version to $$NEW"; \
	git tag "v$$NEW"; \
	echo "Bumped $$CURRENT → $$NEW. Push with: git push && git push --tags"

# Show commits between two tags. Defaults to previous tag → current version.
# Override with: make changelog FROM=v1.0.0 TO=v1.0.2
changelog:
	@FROM=$${FROM:-$(shell git tag --sort=-version:refname | sed -n '2p')}; \
	TO=$${TO:-v$(VERSION)}; \
	echo ""; \
	echo "## $$FROM → $$TO"; \
	echo ""; \
	git log "$$FROM..$$TO" --no-merges --pretty="format:  %s" \
	  | grep -v "^  Bump version"

dist:
	git archive --format=tar.gz --prefix=blue-pearmain-$(VERSION)/ HEAD > $(ARCHIVE)
	@echo "Created $(ARCHIVE)"
	@echo "Contents:"
	@tar -tzf $(ARCHIVE) | head -20
	@echo "..."

test:
	python -m pytest tests/ -q

lint:
	uv run mypy db/ poller/ flickr/ reviewer/ bp
	uv run --with ruff ruff format --check .
	uv run --with ruff ruff check .

install-hooks:
	@for hook in scripts/hooks/*; do \
		name=$$(basename $$hook); \
		ln -sf "../../$$hook" ".git/hooks/$$name" && echo "Installed $$name"; \
	done

clean:
	rm -f blue-pearmain-*.tar.gz
