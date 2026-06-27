# Step4a SFT Selector 使用说明

Step4a 读取 Step3b 的 selector 结果，调用 SFT selector 模型（基于 vLLM 推理），输出最终 SQL JSON。

---

## 1. 安装依赖

直接通过项目根目录的 `requirements.txt` 安装：

```bash
pip install -r requirements.txt
```

Step4a 关键依赖（已在 `requirements.txt` 中锁定，**不要随意升级**）：

| 包 | 版本 | 说明 |
| --- | --- | --- |
| `transformers` | `==4.56.2` | tokenizer 与 chat template；vllm 0.13.0 强制 `>=4.56.0,<5` |
| `vllm` | `==0.13.0` | SFT selector next-token logprob 推理；0.20+ 需要 CUDA 13/驱动 580+，不兼容当前 550.x |
| `flashinfer-python` | `==0.5.3` | vllm 0.13.0 强制依赖 |
| `flashinfer-cubin` | `==0.5.3` | 必须与 `flashinfer-python` 同版本，否则启动报 cubin/version mismatch |

**实测可运行环境**：NVIDIA driver 550.x / CUDA 12.4 / RTX 3090 24G。

> ⚠️ 如果 `pip install` 拉取 `nvidia-cudnn-cu12` 等大包很慢，可换清华源单独装 NVIDIA 系列：
> ```bash
> pip install -i https://pypi.tuna.tsinghua.edu.cn/simple nvidia-cudnn-cu12 nvidia-cublas-cu12 nvidia-cusparse-cu12
> ```

---

## 2. 确认输入

题目文件由 `config.py` 中 `DEV_JSON` 指定。

Step3b selector 结果必须已经存在：

```text
workspace/sql_generation/{step3b_run_id}/selector/q_XXXX_selected.json
```

---

## 3. 下载模型

模型地址：<https://modelers.cn/models/datagallery/selector-8b-0524>（约 16 GB，4 个 safetensors 分片）

**推荐下载到项目内目录**（与下文 §4 `--model-path` 默认一致）：

```text
workspace/sft_selector/model
```

### 方式 A：使用 openmind_hub 从魔乐下载（推荐）

魔乐下载文档：<https://modelers.cn/docs/zh/openmind-hub-client/0.9/basic_tutorial/download.html>

```bash
pip install openmind_hub
mkdir -p workspace/sft_selector/model
export HUB_WHITE_LIST_PATHS="$(readlink -f workspace/sft_selector/model)"

python - <<'PY'
from openmind_hub import snapshot_download

snapshot_download(
    repo_id="datagallery/selector-8b-0524",
    repo_type="model",
    local_dir="workspace/sft_selector/model",
    revision="main",
)
PY
```

### 方式 B：使用 Git LFS 从魔乐下载（备用）

魔乐模型仓库也可以通过 Git LFS 克隆：

```bash
git lfs install
git clone https://modelers.cn/datagallery/selector-8b-0524.git \
    workspace/sft_selector/model
```

### 下载完成后检查

应包含以下文件（共 16 GB）：

```text
workspace/sft_selector/model/
├── config.json
├── generation_config.json
├── tokenizer.json
├── tokenizer_config.json
├── chat_template.jinja
├── model.safetensors.index.json
├── model-00001-of-00004.safetensors  (4.6G)
├── model-00002-of-00004.safetensors  (4.6G)
├── model-00003-of-00004.safetensors  (4.7G)
└── model-00004-of-00004.safetensors  (1.5G)
```

---

## 4. 指定模型路径

在 `config.py` 中设置：

```python
SFT_SELECTOR_RUN_ID = "dev_run_example"
SFT_SELECTOR_MODEL_PATH = "nl2sql/workspace/sft_selector/model"
SFT_SELECTOR_OUTPUT_NAME = "predict_dev_sft_selector.json"
```

也可以运行时通过 `--model-path` 覆盖。

---

## 5. 指定 GPU

Step4a 是单机单卡推理（`tensor_parallel_size=1`、`gpu_memory_utilization=0.85`），运行前**必须先用 `nvidia-smi` 查看 GPU 占用，再通过 `CUDA_VISIBLE_DEVICES` 显式指定一张空闲卡**，否则会默认占用 GPU 0 立即 OOM。

```bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader
```

**示例（以本机当前状态为例，8×RTX 3090 24G）**：

| GPU | 显存使用 | 状态 |
| --- | --- | --- |
| 0–5 | ~24 GB / 24 GB | ❌ 已被其他 vLLM 进程占满，不可用 |
| 6 | ~4 MiB | ✅ 空闲 |
| 7 | ~4 MiB | ✅ 空闲 |

根据上表，挑一张空闲卡导出环境变量即可：

```bash
export CUDA_VISIBLE_DEVICES=7   # 或 6
```

> 实际可用 GPU 编号会随其他任务变化，**每次运行前都应重新 `nvidia-smi` 一次**，按当时空闲情况选择。

---

## 6. 运行

在 `nl2sql` 上层目录执行（注意包名是 `nl2sql.runner.step4a_sft_runner`）：

### 6.1 首次全量执行

```bash
export CUDA_VISIBLE_DEVICES=7

python -m nl2sql.runner.step4a_sft_runner \
    --run-id        {step4a_run_id} \
    --source-run-id {step3b_run_id} \
    --model-path    nl2sql/workspace/sft_selector/model \
    --output-name   predict_dev_sft_selector.json
```

### 6.2 中断后的重跑

若第一次执行中途因为断电 / 进程 kill / 终端关闭等原因中断，只需使用**完全相同的命令**即可续跑（无需额外参数）：

```bash
export CUDA_VISIBLE_DEVICES=7

python -m nl2sql.runner.step4a_sft_runner \
    --run-id        {step4a_run_id} \
    --source-run-id {step3b_run_id} \
    --model-path    nl2sql/workspace/sft_selector/model \
    --output-name   predict_dev_sft_selector.json
```

断点续传行为：

- 脚本启动时自动扫描 `workspace/sft_selector/{run_id}/` 目录；
- 已存在 `q_XXXX_sft.json` 的题目自动跳过；
- 仅对剩余未完成题目进行推理。

### 6.3 仅重新汇总输出（跳过推理）

如果中间结果已全部生成，仅需重新生成汇总输出文件：

```bash
python -m nl2sql.runner.step4a_sft_runner \
    --run-id        {step4a_run_id} \
    --source-run-id {step3b_run_id} \
    --output-name   predict_dev_sft_selector.json \
    --finalize-only
```

---

## 7. 输出位置

| 类型 | 路径 | 说明 |
| --- | --- | --- |
| 最终结果 | `output/{output_name}` | 例：`output/predict_dev_sft_selector.json` |
| 中间结果 | `workspace/sft_selector/{run_id}/` | 每题一份 `q_XXXX_sft.json`，记录 yes/no logprob 与选中 SQL |
| 运行日志 | `log/sft_selector/{run_id}/step4a_sft_YYYYMMDD_HHMMSS.log` | vLLM 启动信息、tokenizer 加载、每题进度、报错栈 |

最终输出 `output/{output_name}` 即可直接喂给后续评测/对比脚本。
