# 开发：make prepare && make build && make test。出包：make prepare && make package && make start。
# prepare 装 devDependencies 和 agentkit；build 只编测试用的 TS/API/TUI；package 再加 build:web。
# 不另写启动器、不生成密钥、不打 agentkit wheel。编译不 omit=dev、也不 prune，node_modules 仍含构建期依赖。
# make start 没有 Make 变量，只调用 ./deploy.sh start：缺 .env 不会启动，缺 apps/web/.env.local 就按 .env 补上。
# ut/st 目标名覆盖 TIER：make ut TIER=st 仍跑 ut。范围目标才读 TIER，默认 test；不写 TIER 时 make test 一起跑 ut 和 st。
# make st 与 make test 的系统测试不能带 -n/--numprocesses（含 -n4、--numprocesses=4）：st 直接失败，test 去掉后仍串行，单元测试可以带。
# PYTEST_ARGS 里的 $ 用单引号：make ut PYTEST_ARGS='-k $HOME'。不要写成 $$。make test 时 -k 同时过滤 ut 和 st。
# NODE_TEST_ARGS 不能一次混给 vitest（api/web）和 node --test（tui）：make ut SCOPE=api NODE_TEST_ARGS='--maxWorkers 2'。
# 例：make prepare PYTHON_VERSION=3.12 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple NPM_REGISTRY=https://registry.npmmirror.com

PROJECT_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

# agentkit 要求 3.12，也可用 UV_PYTHON。
PYTHON_VERSION ?= $(or $(UV_PYTHON),3.12)
export UV_PYTHON := $(PYTHON_VERSION)

# 不设则用 PyPI。
ifdef PIP_INDEX_URL
export UV_DEFAULT_INDEX := $(PIP_INDEX_URL)
export UV_INDEX_URL := $(PIP_INDEX_URL)
endif
ifdef PIP_EXTRA_INDEX_URL
export UV_EXTRA_INDEX_URL := $(PIP_EXTRA_INDEX_URL)
endif
ifdef PIP_FIND_LINKS
export UV_FIND_LINKS := $(PIP_FIND_LINKS)
endif

ifdef NPM_REGISTRY
export npm_config_registry := $(NPM_REGISTRY)
endif

ifeq ($(OS),Windows_NT)
    SHELL := bash.exe
endif

.PHONY: ut st test prepare build package start agentkit api web tui packages deploy

# 例：make test UV_RUN_OPTS=--no-sync（只跳过套件里的再次同步）。
UV := uv run --project "$(PROJECT_DIR)/runtime/agentkit" --python "$(PYTHON_VERSION)" python
TIER ?= test

# 冻结命令行原文，$ 和 $(函数) 不再展开。
override PYTEST_ARGS := $(value PYTEST_ARGS)
override NODE_TEST_ARGS := $(value NODE_TEST_ARGS)

ut st test:
	cd "$(PROJECT_DIR)" && $(UV) scripts/run_product_tests.py --tier $(if $(filter ut st,$(MAKECMDGOALS)),$@,$(TIER)) $(if $(SCOPE),--scope "$(SCOPE)") --pytest-args='$(PYTEST_ARGS)' --node-test-args='$(NODE_TEST_ARGS)' $(UV_RUN_OPTS)

prepare:
	cd "$(PROJECT_DIR)" && uv sync --project runtime/agentkit --python "$(PYTHON_VERSION)"
ifdef NPM_REGISTRY
	cd "$(PROJECT_DIR)" && npm ci --engine-strict=false --registry "$(NPM_REGISTRY)"
else
	cd "$(PROJECT_DIR)" && npm ci --engine-strict=false
endif

# 不含 Next 生产构建。
build:
	cd "$(PROJECT_DIR)" && npm run build
	cd "$(PROJECT_DIR)" && npm run build:tui

# make packages 仍是测试范围。
package: build
	cd "$(PROJECT_DIR)" && npm run build:web

start:
	cd "$(PROJECT_DIR)" && ./deploy.sh start

# 该 scope 没有 st 用例时仍非 0 退出。
agentkit api web tui packages deploy:
	cd "$(PROJECT_DIR)" && $(UV) scripts/run_product_tests.py --tier $(TIER) --scope $@ --pytest-args='$(PYTEST_ARGS)' --node-test-args='$(NODE_TEST_ARGS)' $(UV_RUN_OPTS)
