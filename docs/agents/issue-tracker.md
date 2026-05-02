# Issue tracker: Local Markdown

Issues 和 PRD 以 markdown 文件形式存放在 `.scratch/`。

## 约定

- 每个功能一个目录：`.scratch/<feature-slug>/`
- PRD 放在：`.scratch/<feature-slug>/PRD.md`
- 实现 issue 放在：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`，从 `01` 开始编号
- 状态记录在文件顶部的 `Status:` 行（值为 `todo` / `doing` / `wontfix`）
- 评论追加到文件底部的 `## Comments` 标题下
