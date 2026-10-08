SHELL := /bin/bash
VERSION ?= 0.6.0
.DEFAULT_GOAL := help

BASH_COMPLETION_DIR ?= ~/.bash_completion.d
WITH_VENV = if [ -z "$$VIRTUAL_ENV" ]; then source "$(CURDIR)/mkvenv.sh"; fi

COMMIT ?= HEAD

SCRIPTS = \
    scan_runner \
    actuator_runner \
    scanplotter_cli \
    scantrigger_cli \
    pollstats_cli \
    manifestfiles \
    scanioc \
    kiwi2spec

SCRIPT_PATHS = $(foreach script,$(SCRIPTS),$(BASH_COMPLETION_DIR)/$(script))

.PHONY: help all install_completion uninstall_completion clean cscope tag lint test

help: ## Show available development targets
	@echo "kiwi-scan development targets"
	@echo
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_.-]+:.*## / { printf "  %-22s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

all: install_completion ## Install bash completion scripts

install_completion: ## Install bash completion files and add source lines to ~/.bashrc
	@mkdir -p $(BASH_COMPLETION_DIR)
	@for script in $(SCRIPTS); do \
		cp bash-completion/$$script $(BASH_COMPLETION_DIR)/$$script; \
		grep -q "$$script" ~/.bashrc || echo "source $(BASH_COMPLETION_DIR)/$$script" >> ~/.bashrc; \
	done
	@echo "Bash completion for $(SCRIPTS) installed. Reload your shell to activate."

uninstall_completion: ## Remove installed bash completion files
	@for script in $(SCRIPTS); do \
		rm -f $(BASH_COMPLETION_DIR)/$$script; \
	done
	@echo "Bash completion for $(SCRIPTS) removed. Edit your .bashrc and remove the source lines."

clean: ## Remove local virtualenv, caches, tags, and egg-info
	@rm -rf .venv
	@rm -rf dist
	@rm -f tags cscope.files cscope.out
	@find src -maxdepth 1 -type d -name "*.egg-info" -exec rm -rf {} +
	@find . -type d -name "__pycache__" -exec rm -r {} +

cscope: ## Build cscope and ctags indexes for the repository
	find . \( -path ./.venv -o -path ./build -o -path ./dist \) -prune -o -name "*.py" -print > cscope.files
	cscope -b -i cscope.files
	ctags -R --languages=Python .

tag: ## Create a version+timestamp git tag from COMMIT
	@base="$(VERSION)"; \
	commit="$(COMMIT)"; \
	if git rev-parse "$$base" >/dev/null 2>&1; then \
		echo "ERROR: release tag $$base already exists"; \
		exit 1; \
	fi; \
	ts=$$(git show -s --format=%cd --date=format:%Y%m%d%H%M%S "$$commit"); \
	tag="$$base+$$ts"; \
	if git rev-parse "$$tag" >/dev/null 2>&1; then \
		echo "ERROR: tag $$tag already exists"; \
		exit 1; \
	fi; \
	echo "Creating tag $$tag"; \
	git tag -a "$$tag" "$$commit" -m "Release $$tag"

lint: ## Run pylint, ruff, and pyright (uses mkvenv.sh when needed)
	@echo
	@echo '========================================================================'
	@echo '  PYLINT'
	@echo '========================================================================'
	@$(WITH_VENV); \
	PYTHONPATH=src pylint src/kiwi_scan
	@echo '========================================================================'
	@echo
	@echo '========================================================================'
	@echo '  RUFF'
	@echo '========================================================================'
	@$(WITH_VENV); \
	ruff check src/kiwi_scan tests/
	@echo '========================================================================'
	@echo
	@echo '========================================================================'
	@echo '  PYRIGHT'
	@echo '========================================================================'
	@$(WITH_VENV); \
	pyright src/kiwi_scan
	@echo '========================================================================'
	@echo
	@echo 'Lint complete.'


test: ## Run the current test scripts (uses mkvenv.sh when needed)
	@if [ -z "$$KIWI_SCAN_DATA_DIR" ]; then mkdir -p scandata; fi
	@$(WITH_VENV); \
	PYTHONPATH=src EPICS_WRITETEST=1 python -m pytest --cov=src/kiwi_scan --cov-report=term


