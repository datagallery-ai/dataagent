# BIRD benchmark Suite

安装普通 wheel 后，通过统一入口完成 BIRD 预处理、评测、一次错误补跑和结果汇总。`standard` 与 `limited` 提供可覆盖的运行默认值；每次运行使用一个评测模型，保留原 BirdAgent、SQL 判分及逐题产物。

```sh
python -m pip install /path/to/datagallery_dataagent-0.2.0-py3-none-any.whl
dataagent-bird-benchmark --help
```

等价模块入口：

```sh
python -m dataagent.core.suite.builtin_suites.bird_benchmark.run_bird --help
```

运行环境沿用已有 `nl2sql` 或 `all` 依赖，无新增 BIRD extra。wheel 不包含 BIRD 数据、预处理缓存或密钥，安装后运行不依赖源码 checkout、Git 或 pytest。

`test_bird_e2e.py` 等原独立工具仍可通过包内模块路径调用；`run_bird_dev_models.sh` 仅转换旧环境变量并调用统一入口，不再自动运行两个模型。

预处理、评测、参数和结果说明见 [操作手册](MANUAL.zh-CN.md)。
