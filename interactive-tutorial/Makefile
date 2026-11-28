# interactive-tutorial skill - lint, test, example validation, install.
#
#   make test                                  run the unittest suite
#   make test-corpus                           run the corpus fixture tests
#   make validate-examples                     validate + render examples/
#   make install                               symlink into ~/.agents/skills
#   make install LOCAL=1                       link into ./.agents/skills instead
#   make install SKILLS_DIR=/tmp/skills        anywhere (used by the tests and CI)
#
# No third-party tools: python3 and coreutils only.

PYTHON ?= python3
SKILL_NAME := interactive-tutorial
# Absolute path of the directory holding this Makefile, i.e. the skill itself.
SKILL_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
SKILLS_DIR ?= $(HOME)/.agents/skills

# LOCAL only applies when the caller did not name a directory explicitly, so the
# repository root can translate LOCAL into an absolute path for us.
ifeq ($(LOCAL),1)
ifeq ($(origin SKILLS_DIR),undefined)
SKILLS_DIR := $(CURDIR)/.agents/skills
endif
endif

.PHONY: help all lint check-clean test test-corpus validate-examples install uninstall

help: ## show available targets
	@echo "targets:"
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-18s %s\n", $$1, $$2}'
	@echo "  skill        $(SKILL_DIR)"
	@echo "  skills dir   $(SKILLS_DIR)  (LOCAL=$(if $(LOCAL),$(LOCAL),0))"

all: lint validate-examples test-corpus test ## lint, validate the examples, run the corpus and unit tests

check-clean: ## fail if stray scratch or planning paths exist inside the skill
	@fail=0; \
	for d in .development planning; do \
		if [ -e "$(SKILL_DIR)/$$d" ]; then \
			echo "ERROR: stray path inside skill directory: $(SKILL_DIR)/$$d (remove it to keep $(SKILL_NAME)/ self-contained)" >&2; \
			fail=1; \
		fi; \
	done; \
	exit $$fail

lint: check-clean ## check cleanliness and byte-compile the CLI and tests
	@cd "$(SKILL_DIR)" && $(PYTHON) -m py_compile scripts/dag_manager.py && \
		$(PYTHON) -m compileall -q scripts tests && \
		echo "lint OK ($(PYTHON))"

test: ## run the unittest suite in throwaway git repositories
	@cd "$(SKILL_DIR)" && $(PYTHON) -m unittest discover -s tests -p "test_dag*.py" -v

test-corpus: ## run the corpus fixture tests (tests/corpus)
	@cd "$(SKILL_DIR)" && $(PYTHON) -m unittest tests.test_corpus -v

validate-examples: ## validate and render the reference DAG
	@cd "$(SKILL_DIR)" && $(PYTHON) scripts/dag_manager.py \
		--file examples/expected-dag.json validate && \
		$(PYTHON) scripts/dag_manager.py --file examples/expected-dag.json status >/dev/null && \
		echo "validate-examples OK (examples/expected-dag.json)"

install: ## symlink the skill directory into the skills dir
	@dest="$(SKILLS_DIR)/$(SKILL_NAME)"; \
	mkdir -p "$$(dirname "$$dest")" || exit 1; \
	dest="$$($(PYTHON) -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$$dest")"; \
	src="$$($(PYTHON) -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$(SKILL_DIR)")"; \
	if [ "$$dest" = "$$src" ]; then \
		echo "already installed locally: $$src"; exit 0; \
	fi; \
	if [ -e "$$dest" ] && [ ! -L "$$dest" ]; then \
		if [ -f "$$dest/SKILL.md" ] && \
		   [ "$$($(PYTHON) -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$$dest/SKILL.md")" = \
		     "$$($(PYTHON) -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$$src/SKILL.md")" ]; then \
			echo "already installed locally (symlinked): $$dest"; exit 0; \
		fi; \
		echo "refusing to overwrite real directory $$dest (remove it first)"; exit 1; \
	fi; \
	ln -sfn "$$src" "$$dest"; \
	test -r "$$dest/SKILL.md"; \
	echo "installed $$src -> $$dest"

uninstall: ## remove the symlink created by install
	@dest="$(SKILLS_DIR)/$(SKILL_NAME)"; \
	if [ -L "$$dest" ]; then rm "$$dest"; echo "uninstalled $$dest"; \
	elif [ -e "$$dest" ]; then echo "refusing to remove non-symlink $$dest"; exit 1; \
	else echo "not installed: $$dest"; fi
