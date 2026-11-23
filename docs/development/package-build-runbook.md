# 主包与 UI 包出包操作手册

本文回答“从仓库代码怎样得到可以交付的 `.whl` 和 `.tgz`”。包边界和启动契约见[发布方案](package-distribution.md)。

**当前只有 Python wheel 的出包路径可直接执行。UI 合包脚本和统一出包入口尚未实现。** 下文分别给出当前命令、目标流程和需要实现的脚本，不能把目标流程中的脚本名当成已有文件。

## 1. 先看出包路径

```text
仓库根目录/ ────── uv build ─────────────────────────→ 主包.whl

ui/web/ ── next build ──┐
ui/tui/ ── tsc ─────────┼─→ 整理 UI 发行目录 ── npm pack → UI包.tgz
ui/ 启动器 ──────┘        ↑
                              依赖清单、锁文件、资源
```

`uv build` 和 `npm pack` 只生成文件，不上传包仓库。`npm run build` 只编译项目，也不上传。发布是验收之后的独立步骤。

| 操作 | 当前可用性 | 输出 |
| --- | --- | --- |
| `uv build --wheel` | 已有工具和配置支持 | 当前名为 `datagallery_dataagent-0.1.0-py3-none-any.whl` |
| `npm run build:web` | 已有 | `ui/web/.next/` |
| `npm run build:tui` | 已有 | `ui/tui/dist/` |
| 整理一个完整 UI 发行目录 | 待实现 | 含 Web、TUI、命令入口和运行依赖声明的目录 |
| 在该发行目录执行 `npm pack` | npm 已支持；依赖上一项完成 | `datagallery-dataagent-ui-<版本>.tgz` |
| `npm run package` | 待新增统一入口 | 两个包、文件清单、版本清单及校验值 |

不能直接在当前仓库根目录执行 `npm pack` 来代替 UI 合包。根 package 是开发工作区，文件边界、命令入口和安装钩子都不符合发行设计。

## 2. 现在如何打主包

在仓库根目录执行：

```bash
uv build --wheel --out-dir dist/release/current
```

构建工具读取 `pyproject.toml`，由 hatchling 收集 Python 模块和资源，输出到指定目录。它不需要先构建 Web/TUI，也不会把主包的第三方依赖一起塞进 wheel。

按当前元数据，输出为：

```text
dist/release/current/datagallery_dataagent-0.1.0-py3-none-any.whl
```

wheel 只安装一个顶层包 `dataagent`。服务在 `dataagent/server/`，内置插件在 `dataagent/plugins/`，宿主提示词模板在 `dataagent/assembly/prompts/`。命令入口为 `dataagent = dataagent.cli:main`。

在仓库根运行 `uv run pytest tests/packaging -q`。测试从源码包构建 wheel，在仓库外的独立环境安装，逐个渲染三个宿主模板，并检查插件资源、公开 SDK 和 CLI 入口。

改成目标发行名和版本后，必须先提交对应的 `pyproject.toml` 与锁文件变更，再构建。构建器按元数据生成文件名，不通过重命名旧 wheel 改产品名称或版本。

例如目标元数据为 `datagallery-dataagent`、`0.3.0`，产物才会是：

```text
datagallery_dataagent-0.3.0-py3-none-any.whl
```

## 3. 完成改造后的一键出包

目标是在干净的发布 checkout 中执行：

```bash
# 构建机准备 Node 22+、Python 3.12 和 uv
# 安装 JS 构建依赖；Python 环境在下一步单独准备
SKIP_POSTINSTALL_BUILD=1 npm ci
uv sync --locked

# 以下两个 npm script 待实现
npm run package
npm run package:verify
```

`package` 读取已经提交的两包版本，执行编译和出包，不静默改版本、不更新锁文件、不上传。`package:verify` 对生成的包做安装验证；缺少模型验收凭据时应明确报告未完成的业务门禁，不能把静态检查成功视为可发布。

建议新增以下 scripts。此处是需要加入根 `package.json` 的设计片段：

```json
{
  "scripts": {
    "package": "node scripts/release/build-packages.mjs",
    "package:core": "node scripts/release/build-packages.mjs --only core",
    "package:ui": "node scripts/release/build-packages.mjs --only ui",
    "package:verify": "node scripts/release/verify-packages.mjs"
  }
}
```

首版两包版本相同，统一输出到 `dist/release/<版本>/`。单独出包只更新相应产物，不覆盖另一包；相同版本的既有产物不得静默覆盖，重新尝试应使用新的临时目录。只出一包时，在版本清单中记录它与哪一个已发布对端版本配套验收。

## 4. 一键命令内部执行什么

下面是可审核的分步流程。Shell 示例用于 Bash，按顺序在同一会话执行。正式脚本用 Node 的路径与进程 API 实现相同步骤，避免把 Bash 作为 Windows 用户的运行前提。

### 步骤一：检查版本并准备目录

出包前检查：

- Python 和 UI 发行元数据中的名字、版本与发布任务一致。
- `package-lock.json`、`uv.lock` 及 UI 发行依赖锁文件均已提交且匹配。
- Git 工作区没有会进入包的未提交变更；正式版本的构建提交可追溯。
- 输出目录不会混入其他版本的旧文件。

示例目录变量：

```bash
release_root="$(pwd)"
release_version="0.3.0"
release_dir="$release_root/dist/release/$release_version"
ui_stage="$release_root/dist/package-staging/ui-$release_version"
mkdir -p "$release_dir"
```

变量只用于选择目录，不替代元数据中的版本。正式脚本应创建专用暂存目录，并拒绝复用内容不明的旧目录。

### 步骤二：构建 Python wheel

```bash
uv build --wheel --out-dir "$release_dir"
```

脚本随后打开 wheel 检查 `METADATA`、`entry_points.txt`、插件清单和必要资源。缺文件即失败，不继续生成“发布成功”的摘要。

### 步骤三：编译 Web、TUI 和发行启动器

```bash
npm run build:web
npm run build:tui

# 新增 ui 工作区后提供的构建命令
npm --workspace @datagallery/dataagent-ui run build
```

前两条已有，第三条待实现。UI 工作区的 `build` 只编译统一 CLI 和包内共享代码，不再次递归调用根 `package`。新增工作区时需要更新根 `workspaces` 及 `package-lock.json`。

构建时不注入真实用户配置。Web 的后端地址在启动时通过服务端环境设置，不烘焙进浏览器 JS。构建成功后保留 `BUILD_ID`、服务端清单、路由、chunks 和静态资源；不能只复制 HTML/JS 页面文件而遗漏 Next 运行文件。

### 步骤四：整理 UI 发行目录

新增 `scripts/release/stage-ui.mjs`，提供以下命令契约：

```bash
# 待实现：读取已完成的构建结果，整理发行目录
node scripts/release/stage-ui.mjs \
  --out "$ui_stage" \
  --expected-version "$release_version"
```

该脚本不编译、不发布；它根据明确的映射复制和生成文件：

| 输入 | 发行目录中的位置 | 处理规则 |
| --- | --- | --- |
| `ui/web/.next/` | `dist/web/.next/` | 保留运行产物，排除构建缓存与诊断日志 |
| `ui/web/public/`（若存在） | `dist/web/public/` | 复制运行资源 |
| UI 专用的生产 Next 配置 | `dist/web/next.config.mjs` | 不依赖 TypeScript 编译器或仓库根路径；保留 SSE 不压缩等运行行为 |
| Web 最小项目元数据 | `dist/web/package.json` | 设置正确模块类型，供 Next 识别项目 |
| `ui/tui/dist/` | `dist/tui/` | 收集运行文件，排除测试；源码相对引用需在编译前重构 |
| `ui` 的 CLI 与共享代码产物 | `bin/`、`dist/launcher/` 及共享目录 | CLI 使用 shebang；引用只指向包内文件或声明的依赖 |
| UI 发行元数据 | `package.json` | 按下表生成，不复制根项目元数据 |
| 已审核的 UI 发行依赖锁 | `npm-shrinkwrap.json` | 与发行依赖和版本匹配，不在出包时重新解析最新版 |
| 版本与协议元数据 | `release-info.json` | 写入版本、提交和兼容范围 |
| UI 安装说明和许可证 | `README.md`、`LICENSE` | 随包交付 |

发行 `package.json` 至少明确：

| 字段 | 值或生成规则 |
| --- | --- |
| `name` | `@datagallery/dataagent-ui`，以最终确认名为准 |
| `version` | 读取已提交的 UI 版本 |
| `type` | `module`；TUI 与 Next 产物的模块类型需在各自边界正确设置 |
| `bin` | `{"dataagent-ui": "bin/dataagent-ui.mjs"}` |
| `files` | `bin`、`dist`、`release-info.json`、`README.md`、`LICENSE`；锁文件也必须出现在最终包中 |
| `engines` | 记录验收过的 Node 范围 |
| `dependencies` | Web/TUI/启动器真实需要的运行依赖，Next 版本与编译版本一致 |
| `overrides` | 将构建时必要的根级依赖覆盖规则明确带入发行依赖策略 |
| `scripts` | 不带项目构建、Python 安装和配置写入钩子 |

发行对象中不保留 `private: true`、`workspaces` 或 `workspace:`/本地 `file:` 依赖。开发仓库根和 Web/TUI 工作区仍可保持 private。

源码版 TUI 从 `ui/shared/` 导入日志、监听地址和 Python 环境辅助函数。独立 UI 出包时需一并收集这些模块，并让启动器定位已安装的后端。Web 也需检查服务端外部依赖，不能从源码 package.json 简单取并集就认定依赖完整。

### 步骤五：冻结 UI 运行依赖

发行锁文件是受版本控制的输入，不是每次出包临时下载“最新兼容版本”的结果。首次建立或升级依赖时，在独立临时目录放入生成的发行 `package.json`，执行：

```bash
npm install --package-lock-only --ignore-scripts
npm shrinkwrap
```

随后审核并验收生成的 `npm-shrinkwrap.json`，将其保存为 UI 发行输入。版本变动时同步更新其中的根包元数据。日常出包只复制这个锁，验证与 `package.json` 一致。

需要实际安装依赖进行审核时，在发行暂存目录执行 `npm ci --omit=dev`。该目录的 package.json 不含开发依赖和安装钩子。第三方安装脚本按目标平台验收；不能用 `--ignore-scripts` 的安装结果代替需要安装脚本的真实运行验收。

### 步骤六：生成 npm tgz

完成上述整理后执行：

```bash
(
  cd "$ui_stage"
  npm pack --dry-run --json --ignore-scripts > "$release_dir/ui-files.json"
)
```

自动检查 `ui-files.json` 中至少包含命令入口、Web `BUILD_ID`、静态资源、服务端文件、TUI 入口和发行锁。检查文件列表没有配置密钥、会话、测试结果和源码仓库路径。

检查通过后：

```bash
(
  cd "$ui_stage"
  npm pack --json --ignore-scripts --pack-destination "$release_dir" \
    > "$release_dir/ui-pack.json"
)
```

`npm pack` 根据已整理的目录生成 `.tgz`，不会替你编译 TS、修复引用或合并 workspace。根级 `files` 白名单仍要以实际 tarball 为准，特别要验证隐藏目录 `.next`。它也不会默认将所有第三方依赖打进 tgz；用户安装时由 npm 根据发行依赖与锁文件安装。

### 步骤七：生成交付清单

`build-packages.mjs` 从实际 wheel 和 tgz 的元数据读取名称和版本，计算 SHA-256，最后写入 manifest 和校验文件。不要仅从源码版本号推断打出来的包正确。

目标输出：

```text
dist/release/0.3.0/
  datagallery_dataagent-0.3.0-py3-none-any.whl
  datagallery-dataagent-ui-0.3.0.tgz
  release-manifest.json
  SHA256SUMS
  ui-files.json
  ui-pack.json
  verification.json          # 验收脚本完成后写入
```

交付两个包、manifest、校验值及安装说明；文件清单和验证记录也可随附。`dist/package-staging/` 是构建中间目录，不作为用户安装包交付。

### 交付目录中的 JSON 分别记录什么

这四个 JSON 都是发布辅助记录，不是用户配置，不参与产品启动。其中 `ui-files.json`、`ui-pack.json` 是 npm 的原始输出；`release-manifest.json`、`verification.json` 是本方案定义的格式，尚待脚本实现。

| 文件 | 生成者 | 回答的问题 |
| --- | --- | --- |
| `release-manifest.json` | 出包脚本 | 这个版本由哪两个包组成，来自哪个提交，需要什么环境？ |
| `ui-files.json` | `npm pack --dry-run --json` | 准备放进 npm 包的文件有哪些？ |
| `ui-pack.json` | `npm pack --json` | 实际生成的 npm 包叫什么、多大、内容和完整性信息是什么？ |
| `verification.json` | 验收脚本 | 哪一批包在哪些环境完成了哪些验证，是否可以发布？ |

下面的自定义 YAML 为目标结构示例，尖括号内容需要由脚本填写。npm 示例来自仅含两个文件的格式验证包，不代表实际 UI 的内容和大小。

#### release-manifest.json

```json
{
  "schema_version": 1,
  "release_id": "0.3.0",
  "git_commit": "<完整 Git 提交 SHA>",
  "packages": [
    {
      "role": "core",
      "name": "datagallery-dataagent",
      "version": "0.3.0",
      "file": "datagallery_dataagent-0.3.0-py3-none-any.whl",
      "sha256": "<wheel 文件的 64 位十六进制 SHA-256>",
      "requires_python": ">=3.12,<3.13"
    },
    {
      "role": "ui",
      "name": "@datagallery/dataagent-ui",
      "version": "0.3.0",
      "file": "datagallery-dataagent-ui-0.3.0.tgz",
      "sha256": "<tgz 文件的 64 位十六进制 SHA-256>",
      "requires_node": ">=22"
    }
  ],
  "compatibility": {
    "core_api_version": "1.0.0",
    "ui_api_range": ">=1.0.0 <2.0.0",
    "config_schema_version": 4,
    "plugin_schema_version": 3
  },
  "build": {
    "platform": "<构建机 OS/架构>",
    "python": "<实际版本>",
    "node": "<实际版本>",
    "npm": "<实际版本>",
    "uv": "<实际版本>"
  },
  "validation_policy": {
    "target_platforms": ["linux-x64", "darwin-arm64", "win32-x64"],
    "required_checks": [
      "core-install",
      "ui-install",
      "web-connect",
      "tui-connect",
      "managed-backend",
      "live-business",
      "upgrade-rollback"
    ]
  },
  "migration": {
    "required": false,
    "instructions_file": null
  }
}
```

字段规则：

- `schema_version` 是 manifest 自身格式版本；`release_id` 是这组交付的标识。它不替代每个包自己的 `version`。
- `packages` 的名称、版本、运行环境要求从实际包元数据读取；`sha256` 对成品字节计算，不能由文件名推导。`file` 只允许交付目录中的相对文件名。
- `compatibility` 来自两包的协议与 schema 定义，出包时检查一致性。示例中的协议版本仍是待实现契约。
- `build` 记录实际构建工具，不列安装在用户机器上才需要填写的配置。
- `validation_policy` 是本次发布必须验证的平台和项目，示例列表不是当前支持承诺。不同平台的 Node/Python 版本组合可在验收任务中展开；不得用一个平台的通过替代其他目标平台。
- `migration.required` 必须按发布变更确定；需要迁移时指向随交付提供的说明，不固定为示例的 `false`。

manifest 在验收前固定，验收脚本读取它并在报告中记录它的 SHA-256。若包、兼容范围、目标平台或验收要求改变，重新生成 manifest 并重新执行受影响的验收。manifest 不预先写入“已通过”的结论。

#### ui-files.json

该文件直接保存 `npm pack --dry-run --json --ignore-scripts` 的标准输出，不自行改写 npm 字段。npm 11.11.0 的格式验证输出如下；哈希用说明文字替代：

```json
[
  {
    "id": "@datagallery/dataagent-ui@0.3.0",
    "name": "@datagallery/dataagent-ui",
    "version": "0.3.0",
    "size": 271,
    "unpackedSize": 218,
    "shasum": "<npm 计算的 SHA-1>",
    "integrity": "sha512-<npm 计算的 Base64 摘要>",
    "filename": "datagallery-dataagent-ui-0.3.0.tgz",
    "files": [
      {"path": "bin/dataagent-ui.mjs", "size": 51, "mode": 420},
      {"path": "package.json", "size": 167, "mode": 420}
    ],
    "entryCount": 2,
    "bundled": []
  }
]
```

顶层是数组，即使只打一个包也如此。`files` 是包内路径、文件字节数和权限；`mode` 是十进制权限值，例如 `420` 对应八进制 `0644`，`493` 对应 `0755`。`size` 是压缩包字节数，`unpackedSize` 是包中文件未压缩字节数，`entryCount` 是文件条目数。

`bundled` 表示随包捆绑的 npm 依赖，不是全部运行依赖列表。为空不代表 UI 不依赖 Next、React 等库。实际 UI 的文件清单必须包含 Web/TUI 产物；不能将这个最小格式示例当作合格 UI 包。

dry-run 也可以给出体积和完整性字段，但它不写出交付 tgz。该文件用来提前检查漏打或误打的文件。

#### ui-pack.json

该文件直接保存实际 `npm pack --json --ignore-scripts` 的输出。**结构与上面的 `ui-files.json` 相同**，同样包含 `filename`、`size`、`files`、`shasum` 和 `integrity`，不是另一套精简字段。

差别在于生成时机：`ui-files.json` 是出包前的预览；`ui-pack.json` 对应已经写出的 tgz。在已验证的 npm 版本中，输入不变时两份 JSON 可以完全相同。脚本应比较文件路径、大小等关键内容，并以实际 tgz 的内容和摘要为准；不能仅因 dry-run 成功就视为实际包已生成。

npm 的 `shasum` 为 SHA-1，`integrity` 使用带算法前缀的完整性格式；manifest 和 `SHA256SUMS` 另外统一使用 SHA-256。不要把它们当成同一个字段相互比较。

#### verification.json

这是验收记录，不是构建记录。下面是一个尚未完成验收的结构示例，不代表任何产品测试已经通过：

```json
{
  "schema_version": 1,
  "release_id": "0.3.0",
  "manifest_sha256": "<本次读取的 manifest 文件 SHA-256>",
  "status": "incomplete",
  "publishable": false,
  "artifact_checks": [
    {"id": "checksums", "status": "not_run", "reason": null},
    {"id": "package-contents", "status": "not_run", "reason": null}
  ],
  "platform_results": [
    {
      "platform": "linux-x64",
      "environment": {
        "python": null,
        "node": null,
        "npm": null
      },
      "checks": [
        {"id": "core-install", "status": "not_run", "reason": null},
        {"id": "ui-install", "status": "not_run", "reason": null},
        {"id": "web-connect", "status": "not_run", "reason": null},
        {"id": "tui-connect", "status": "not_run", "reason": null},
        {"id": "managed-backend", "status": "not_run", "reason": null},
        {
          "id": "live-business",
          "status": "skipped",
          "reason": "未提供显式测试模型配置"
        },
        {"id": "upgrade-rollback", "status": "not_run", "reason": null}
      ]
    }
  ]
}
```

单项状态为 `passed`、`failed`、`skipped`、`not_run`。实际运行后填写环境版本，并可增加耗时、脱敏错误摘要、相对日志路径等证据。缺少目标平台记录或必需测试项，按未完成处理，不能默认为通过。

汇总规则固定如下：有失败项为 `failed`；没有失败但缺少必需结果为 `incomplete`；产物检查及所有目标平台的必需项目全部通过才为 `passed`。只有最后一种情况 `publishable` 才能为 `true`。必需真实模型测试因缺配置被跳过时，包可以保留为候选，但不能标记可正式发布。

发布作业重新核对包哈希、manifest 哈希及这些规则，不只相信可被手工改写的 `publishable` 布尔值。验收报告不写入测试密钥、原始对话、数据库结果或个人绝对路径。

#### SHA256SUMS 与这些文件的关系

`SHA256SUMS` 是纯文本，不是 JSON。每行采用“64 位摘要、两个空格、相对文件名”的格式，至少覆盖 wheel、tgz 和已固定的 manifest。它不包含自己的摘要。

生成顺序为：两个包 → manifest → SHA256SUMS → 验收报告。首版不将之后才生成的 `verification.json` 放进这份校验文件，避免循环依赖；验收报告通过 `manifest_sha256` 绑定所验收的产物。

用户安装和启动只需要两个产品包及其运行依赖。这些辅助文件用于说明、核对和支持；`ui-files.json`、`ui-pack.json`、`verification.json` 可以只保留在发布作业中，不必要求用户处理。

## 5. 怎样验证打出来的包能安装

目标 `verify-packages.mjs` 要完成三个独立环境的检查：

1. **Python 环境**：只安装 wheel，验证初始化、命令入口、插件资源和 API。
2. **Node 环境**：只安装 tgz，连接已有后端，验证 Web/TUI，不依赖本机 Python。
3. **联合环境**：安装两包，验证 `--start-backend`、异常清理与会话行为。

开发机可先做如下安装冒烟。以下示例使用目标包名和新命令，需在改造完成后运行；隔离环境位于源码目录之外：

```bash
verify_dir="$(mktemp -d)"
python3.12 -m venv "$verify_dir/python"
"$verify_dir/python/bin/python" -m pip install \
  "$release_dir/datagallery_dataagent-0.3.0-py3-none-any.whl"
npm install -g --prefix "$verify_dir/node" \
  "$release_dir/datagallery-dataagent-ui-0.3.0.tgz"

export DATAAGENT_HOME="$verify_dir/home"
cd "$verify_dir"
"$verify_dir/python/bin/dataagent" --version
"$verify_dir/python/bin/dataagent" init
"$verify_dir/node/bin/dataagent-ui" --version
```

然后填入测试模型配置，前台启动并验证：

```bash
"$verify_dir/node/bin/dataagent-ui" web --start-backend \
  --backend-python "$verify_dir/python/bin/python"

# Web 验证结束后退出，再验证 TUI
"$verify_dir/node/bin/dataagent-ui" tui --start-backend \
  --backend-python "$verify_dir/python/bin/python"
```

Windows 验收由脚本使用对应的 `Scripts`、npm shim 路径执行，不照抄上面的 Unix 路径。正式平台 CI 还需让源码目录不可访问、清除 `PYTHONPATH`/`NODE_PATH` 等开发注入、验证无另一种语言环境的安装组合。仅在源码工作区外换一个 cwd 不能证明完全脱离源码依赖。

端口 8790/3000 被占用时使用隔离配置和测试端口，不结束现有用户服务。`verification.json` 分别记录产物检查、安装冒烟、平台测试和真实业务验收状态；不把部分成功合并成一个模糊的 passed。

## 6. 出包脚本需要实现到什么程度

| 脚本 | 输入 | 必须完成 | 失败行为 |
| --- | --- | --- | --- |
| `build-packages.mjs` | 固定提交、元数据、锁文件，可选 `--only core/ui` | 校验版本 → 编译 → wheel → stage UI → pack → 校验值与 manifest | 任一步失败即非零退出，保留诊断，不上传 |
| `stage-ui.mjs` | 构建产物、发行元数据、`--out`、`--expected-version` | 按映射收集、生成运行配置、检查资源与依赖闭包 | 缺文件、版本不符、包外引用即失败 |
| `verify-packages.mjs` | release manifest、实际两个包、测试配置 | 校验哈希 → 隔离安装 → 命令/API/UI/生命周期验证 → 验收报告 | 未完成的测试单独报告；正式发布门禁未满足时非零退出 |

测试阶段读取模型凭据的方式与产品一致，不把凭据写入暂存目录。出包脚本不包含发布凭据，不修改用户 Home，不顺便执行 `git push`、`uv publish` 或 `npm publish`。

## 7. 验收后如何上传

确认仓库权限、版本和目标 registry 后，可以上传已经验收的文件。下面是发布作业命令示例，不属于构建脚本：

```bash
# PyPI 或配置好的 Python registry
uv publish "$release_dir/datagallery_dataagent-0.3.0-py3-none-any.whl"

# 公共 npm scoped package；私有包使用对应的访问设置
npm publish "$release_dir/datagallery-dataagent-ui-0.3.0.tgz" \
  --access public --ignore-scripts
```

registry 与认证由发布作业的配置提供，不能在文档或源码中写真实 token。上传的是刚才验收过的文件，不在发布命令中重新构建。发布后再从 registry 安装固定版本，核对哈希/包元数据和命令入口。

如果通过文件交付给用户，可以不执行上传步骤。用户直接 `pip install ./包.whl` 和 `npm install -g ./包.tgz`，其依赖下载条件与联网安装相同。

## 8. 工具依据

- [uv 构建和发布](https://docs.astral.sh/uv/guides/package/)：构建 wheel 与上传 registry 是两个动作。
- [npm pack](https://docs.npmjs.com/cli/v11/commands/npm-pack/)：文件清单预览、输出 tarball、指定产物目录。
- [npm shrinkwrap](https://docs.npmjs.com/cli/v11/configuring-npm/npm-shrinkwrap-json/)：应用发行所用的依赖锁文件。

正式作业固定工具版本，并对该版本运行帮助和成品安装验证；文档中的目标脚本在实现前不具备可执行能力。
