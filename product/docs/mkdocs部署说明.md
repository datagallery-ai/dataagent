## 概述
mkdocs 是一个快速、简单、华丽的静态网站生成器，可以用于构建项目文档。   

## 操作步骤
```bash
# 进入 DataAgent 项目根目录（把 <dataagent-root> 换成实际路径）
cd <dataagent-root>

# 激活虚拟环境（前提是已在根目录创建过 .venv）
source .venv/bin/activate

# 安装可选依赖（mkdocs 相关依赖在项目的 optional extra `mkdoc` 里）
uv sync --extra mkdoc

# 启动本地预览服务（默认 http://127.0.0.1:8000/）
uv run mkdocs serve -f docs/mkdocs.yml
```

可以通过 http://127.0.0.1:8000/ 访问网站   
【注】如果有端口冲突，可以执行以下命令   
uv run mkdocs serve -f docs/mkdocs.yml -a 127.0.0.1:端口号
