# 翻译响应与状态格式

请求 JSONL 每行是独立 JSON 对象：

```json
{"id":"片段标识","text":"费用为 ⟦P0000⟧。","context":"费用说明","terms":[],"occurrences":1}
```

保护标记在译文中必须完整且各出现一次；可以随句式移动。未在请求出现的保护标记禁止引入。带语义的保护内容另列在请求的 `protected` 中，辅助理解；正文仍使用标记。

响应 JSONL：

```json
{"id":"片段标识","translation":"The fee is ⟦P0000⟧.","review":{"status":"PASS","method":"contextual","notes":"核对费用主体与金额含义"}}
```

`review.status` 为 PASS、NEEDS_CONFIRMATION 或 UNVERIFIED。`method` 和 `notes` 记录实际审校方法与理由。不得用固定相同文案冒充完成逐段语义检查。没有 review 时机器检查仍可运行，但语义结论 UNVERIFIED。存在歧义可提供暂定译法并标 NEEDS_CONFIRMATION。

Word 节点的首尾空白须原样保留，否则会破坏相邻不同样式的文字连接。跨节点句子难以自然翻译时标记待适配，不强行拼接。

如原文确应保留，返回 `keep_original: true` 并填写 `reason`。否则与原文完全相同会触发待确认，而非自动认定漏译。用户确认使用指定译文后由 confirm 命令固定；响应不能自行声明用户 confirmed。

工作目录：

```text
translation-work/
  manifest.json                 当前任务、输入摘要、片段与译文状态
  summary.json                  本轮预检、复用和待译统计
  requests/batch-0001.jsonl      紧凑待译批次；重跑 prepare 刷新
  ingest-report.json            最近一次导入结果
  translated/                  完整且机械检查通过的文件
  reports/translation-report.json
  reports/translation-report.html
```

manifest 不存语言模型或平台元数据。缓存键包含任务配置、原文、上下文。confirmed 内容遇到规则变化进入 conflicts；需要显式 resolve 后才继续。

长段落不硬截断。单条超出默认批次字符预算时独占批次，由 Agent 保持上下文处理。响应可分多次导入；不能为凑齐文件而填假译文。
