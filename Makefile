# 流水线：make prepare && make build && make test。仅编译前端：npm run build。源码启动：make start。
# prepare 分别执行 npm install 和 uv sync --no-dev。build 编译各组件。
# Server wheel 在 dist/server，TUI npm 包在 dist/tui。Web 使用原位编译结果。
# 编译不 omit=dev、也不 prune，node_modules 仍含构建期依赖。
# make start 调用 ./deploy.sh start，使用已有 Python 环境和 Web 构建结果。
# ut/st 固定测试层级；make test 和范围目标读 TIER，默认同时运行 ut 和 st。
# make st 与 make test 的系统测试不能带 -n/--numprocesses（含 -n4、--numprocesses=4）：st 直接失败，test 去掉后仍串行，单元测试可以带。
# PYTEST_ARGS 里的 $ 用单引号：make ut PYTEST_ARGS='-k $HOME'。不要写成 $$。make test 时 -k 同时过滤 ut 和 st。
# 按测试工具选择 NODE_TEST_ARGS，例如 make ut SCOPE=web NODE_TEST_ARGS='--maxWorkers 2'。API 测试使用 PYTEST_ARGS。
# 例：make prepare UV_PYTHON=3.12 UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple npm_config_registry=https://registry.npmmirror.com

PROJECT_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

export UV_PYTHON ?= $(or $(PYTHON_VERSION),3.12)

ifeq ($(OS),Windows_NT)
    SHELL := bash.exe
endif

.PHONY: ut st test prepare build start agentkit api web tui deploy

# 测试入口准备开发依赖，套件不再隐式同步；UV_RUN_OPTS=--no-sync 跳过准备。
TEST_RUNNER := uv run --no-project python scripts/run_product_tests.py
TIER ?= test
ifeq ($(strip $(value UV_RUN_OPTS)),)
TEST_SYNC_OPTION :=
else ifeq ($(strip $(value UV_RUN_OPTS)),--no-sync)
TEST_SYNC_OPTION := --no-sync
else
TEST_SYNC_OPTION = $(error UV_RUN_OPTS only accepts --no-sync)
endif

# 冻结命令行原文，$ 和 $(函数) 不再展开。
override PYTEST_ARGS := $(value PYTEST_ARGS)
override NODE_TEST_ARGS := $(value NODE_TEST_ARGS)
quote = '$(subst ','"'"',$(1))'

ut st test:
	cd "$(PROJECT_DIR)" && $(TEST_RUNNER) --tier $(if $(filter ut st,$@),$@,$(TIER)) $(if $(SCOPE),--scope "$(SCOPE)") --pytest-args=$(call quote,$(PYTEST_ARGS)) --node-test-args=$(call quote,$(NODE_TEST_ARGS)) $(TEST_SYNC_OPTION)

prepare:
	cd "$(PROJECT_DIR)" && npm install --include=dev && uv sync --no-dev

build:
	cd "$(PROJECT_DIR)" && \
		uv build --project . --wheel --clear --out-dir dist/server && \
		npm run build && node scripts/package-tui.mjs

start:
	cd "$(PROJECT_DIR)" && ./deploy.sh start

# 以下目标按组件运行测试；真正部署使用 ./deploy.sh deploy。该范围没有所选层级时返回非零。
agentkit api web tui deploy:
	cd "$(PROJECT_DIR)" && $(TEST_RUNNER) --tier $(TIER) --scope $@ --pytest-args=$(call quote,$(PYTEST_ARGS)) --node-test-args=$(call quote,$(NODE_TEST_ARGS)) $(TEST_SYNC_OPTION)
