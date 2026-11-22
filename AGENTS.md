# 项目协作约定

- 项目及前端展示名称为 **AI audio service**。
- 每批完成并验证的修改都创建本地 Git commit，按逻辑相关的改动分组，方便回退。
- 提交前检查工作区与暂存区，只包含本批相关文件，保留用户已有改动。
- commit 不代表允许远程同步。每次 push、pull、fetch、remote update 或等价操作，都需要用户对本次操作的明确批准。
- merge、rebase、reset、cherry-pick 等操作也需要对应的用户指示。
- 完成时报告本地 commit ID 和远程同步状态；未同步时明确说明“仅本地提交，尚未同步远程”。
- 代码文件开头用中文一句话说明职责；Python 命名函数使用 Google 风格 docstring，按需说明参数、返回值与异常；TypeScript/JavaScript 使用 JSDoc 说明对应信息，包含辅助函数与方法。
- `output/` 和 `docs/` 仅保存在本地，不纳入 Git 提交；踩坑和实测记录继续写入本地 `docs/`，原始输出写入 `output/`。仓库保留 README 中的运行方式与核心架构说明。
