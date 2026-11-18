# 开发：make prepare && make build && make test。AgentKit 出包：make package。源码启动：make start。
# prepare 执行 npm ci。安装后的同步只装 agentkit 运行依赖，不含 pytest/ruff；读 UV_PYTHON，默认 3.12，且不受 SKIP_POSTINSTALL_BUILD 影响。build 编译网页和 TUI。
# package 调用 npm run package:agentkit，生成 wheel、源码包和依赖约束到 dist/agentkit；需要 Node.js 和 uv，不编译前端。
# 编译不 omit=dev、也不 prune，node_modules 仍含构建期依赖。
# make start 没有 Make 变量，只调用 ./deploy.sh start：缺 .env 或网页构建结果不会启动。
# ut/st 目标名覆盖 TIER：make ut TIER=st 仍跑 ut。范围目标才读 TIER，默认 test；不写 TIER 时 make test 一起跑 ut 和 st。
# make st 与 make test 的系统测试不能带 -n/--numprocesses（含 -n4、--numprocesses=4）：st 直接失败，test 去掉后仍串行，单元测试可以带。
# PYTEST_ARGS 里的 $ 用单引号：make ut PYTEST_ARGS='-k $HOME'。不要写成 $$。make test 时 -k 同时过滤 ut 和 st。
# NODE_TEST_ARGS 不能一次混给 vitest（api/web）和 node --test（tui）：make ut SCOPE=api NODE_TEST_ARGS='--maxWorkers 2'。
# 例：make prepare PYTHON_VERSION=3.12 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple NPM_REGISTRY=https://registry.npmmirror.com

PROJECT_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

# agentkit 要求 3.12，也可用 UV_PYTHON。
export UV_PYTHON ?= $(or $(PYTHON_VERSION),3.12)

# 不设则用 PyPI。
ifdef PIP_INDEX_URL
export UV_DEFAULT_INDEX ?= $(or $(UV_INDEX_URL),$(PIP_INDEX_URL))
endif
ifdef PIP_EXTRA_INDEX_URL
export UV_INDEX ?= $(or $(UV_EXTRA_INDEX_URL),$(PIP_EXTRA_INDEX_URL))
endif
ifdef PIP_FIND_LINKS
export UV_FIND_LINKS ?= $(PIP_FIND_LINKS)
endif

ifdef NPM_REGISTRY
export npm_config_registry ?= $(or $(NPM_CONFIG_REGISTRY),$(NPM_REGISTRY))
endif

ifeq ($(OS),Windows_NT)
    SHELL := bash.exe
endif

.PHONY: ut st test prepare build package start agentkit api web tui deploy

# 测试入口准备开发依赖，套件不再隐式同步；UV_RUN_OPTS=--no-sync 跳过准备。
TEST_RUNNER := npm run test:product --
TIER ?= test

# 冻结命令行原文，$ 和 $(函数) 不再展开。
override PYTEST_ARGS := $(value PYTEST_ARGS)
override NODE_TEST_ARGS := $(value NODE_TEST_ARGS)

ut st test:
	cd "$(PROJECT_DIR)" && $(TEST_RUNNER) --tier $(if $(filter ut st,$(MAKECMDGOALS)),$@,$(TIER)) $(if $(SCOPE),--scope "$(SCOPE)") --pytest-args='$(PYTEST_ARGS)' --node-test-args='$(NODE_TEST_ARGS)' $(UV_RUN_OPTS)

prepare:
	cd "$(PROJECT_DIR)" && env -u SKIP_POSTINSTALL_BUILD npm ci

build:
	cd "$(PROJECT_DIR)" && npm run build

package:
	cd "$(PROJECT_DIR)" && npm run package:agentkit

start:
	cd "$(PROJECT_DIR)" && ./deploy.sh start

# 该 scope 没有 st 用例时仍非 0 退出。
agentkit api web tui deploy:
	cd "$(PROJECT_DIR)" && $(TEST_RUNNER) --tier $(TIER) --scope $@ --pytest-args='$(PYTEST_ARGS)' --node-test-args='$(NODE_TEST_ARGS)' $(UV_RUN_OPTS)
