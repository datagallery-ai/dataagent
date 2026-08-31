# 参与贡献

DataFoundry 欢迎能够改进工作台、文档、示例、连接器和运行时可靠性的聚焦贡献。

## 可以贡献什么

- 让首次体验更清晰的产品和文档修复。
- 带有日志、trace 和截图的可复现缺陷。
- 数据源或连接器改进，并附带清楚的配置和验证说明。
- 展示团队如何使用 DataFoundry 处理真实数据 Agent 工作流的场景案例。
- 保护核心行为的测试和 smoke 检查，例如只读 SQL 执行、数据源注册和过程追溯。

## Pull request 期望

Pull request 应尽量小而清晰：

1. 关联 issue，或说明用户可感知的问题。
2. 解释实现范围。
3. 包含测试、smoke 检查或文档验证。
4. 说明兼容性、迁移或部署影响。
5. 避免混入无关格式化或重构。

提交 pull request 前，请先阅读仓库级 [CONTRIBUTING.md](https://github.com/datagallery-lab/datafoundry/blob/main/CONTRIBUTING.md)。

## 本地验证

仅修改文档时，运行：

```bash
npm run docs:build
npm run smoke:docs
```

如果修改产品或运行时逻辑，请在 pull request 描述里补充对应的 build、typecheck、test 或 smoke 命令。
