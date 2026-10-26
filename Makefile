# Repository container for the interactive-tutorial skill.
#
# The real skill lives in ./interactive-tutorial/ so it can be copied into
# another repo and still build, test, and install itself. The Agent Skills
# discovery path .agents/skills/interactive-tutorial/ contains symlinks back to
# it. This file just forwards.
#
#   make all                       lint + validate-examples + test
#   make test                      unittest suite
#   make validate-examples         validate + render the reference DAG
#   make lint                      byte-compile scripts and tests
#   make install                   symlink the skill into ~/.agents/skills
#   make install LOCAL=1           link it into this repo's .agents/skills (no-op)
#   make install SKILLS_DIR=/tmp/x link it somewhere else
#   make uninstall                 remove that symlink

SKILL_DIR ?= interactive-tutorial
TARGETS := all lint test validate-examples install uninstall

.DEFAULT_GOAL := all

# Translate LOCAL into an absolute skills directory once, here, so the skill's
# own Makefile never has to guess which repository invoked it.
ifeq ($(LOCAL),1)
export SKILLS_DIR := $(CURDIR)/.agents/skills
endif

.PHONY: $(TARGETS) help

$(TARGETS):
	@$(MAKE) --no-print-directory -C "$(SKILL_DIR)" $@

help:
	@echo "interactive-tutorial repository container"
	@echo "  skill:   $(SKILL_DIR)"
	@echo "  targets: $(TARGETS)"
	@echo "  run 'make -C $(SKILL_DIR) help' for the skill's own targets"
