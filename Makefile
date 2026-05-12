VERSION := $(shell grep '^version' pyproject.toml | sed 's/.*= *"\(.*\)"/\1/')
ARCHIVE := blue-pearmain-$(VERSION).tar.gz

.PHONY: dist test clean install-hooks

dist:
	git archive --format=tar.gz --prefix=blue-pearmain-$(VERSION)/ HEAD > $(ARCHIVE)
	@echo "Created $(ARCHIVE)"
	@echo "Contents:"
	@tar -tzf $(ARCHIVE) | head -20
	@echo "..."

test:
	python -m pytest tests/ -q

install-hooks:
	@for hook in scripts/hooks/*; do \
		name=$$(basename $$hook); \
		ln -sf "../../$$hook" ".git/hooks/$$name" && echo "Installed $$name"; \
	done

clean:
	rm -f blue-pearmain-*.tar.gz
