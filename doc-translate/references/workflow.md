# 执行与交付协议

## 最小入口

“把这个文件翻译成英文，保留格式”足以开始。未提供目标语言且上下文也无法推断时才询问。默认按原文风格，不要求用户选模式、写 JSON 或运行命令。

首次翻译和增量翻译均由 Agent 调用 prepare → 翻译批次 → ingest → build。目标语言用明确标签，例如 en、zh-CN、ja。单文件或目录均可；目录保留相对路径，跳过软链接和系统垃圾，未知格式列入阻断清单。

```bash
python3 scripts/translate.py prepare --input "资料" --target en --work "translation-work" --glossary "glossary.json" --context "面向企业用户的产品帮助文档，简明正式"
```

工作目录必须位于输入目录外。prepare 发现已有进度时自动复用目标语言、术语及场景参数。输入根路径不能偷偷改变。更换语言或场景、修改术语将使旧缓存失效；用户确认过的旧译文会列为冲突，而非自动重译。可将同一项目复制为独立工作目录处理另一目标语言。

## 批量、恢复与术语

1. 读 `summary.json`：文件阻断、格式限制、确认译文冲突和字符统计优先处理。
2. 按需读取 `requests/batch-0001.jsonl` 等。每条包含 ID、mask 后文本、上下文、相关术语和重复引用数。
3. 只翻译 pending 请求。候选术语不强行锁定，已确认术语以保护标记回填，确保一致。
4. 输出 [协议](protocol.md) 指定 JSONL；每个响应均基于该条上下文检查语义。模型无把握的译法标 needs_confirmation。
5. ingest 校验通过项立即保存，失败项保持原状态，错误清单进入 `ingest-report.json`。重复导入同样内容不重复追加；与 confirmed 译文不同的响应会被拒绝。
6. 对失败项修复后再次导入；build 为完整文件生成译文，不完整文件不生成“部分译文”冒充交付。
7. 对冲突保留人工决策：使用 `resolve --id ID --choice keep` 明确保留旧译文并重新校验，或 `--choice retranslate` 将该项重新送译。只有用户作出该选择后再调用。

用户确认译文后可使用 `confirm --id ID` 固定。修正它需 `resolve --choice retranslate`；该操作必须符合用户实际修改意图。

## 已有译文审校

```bash
python3 scripts/translate.py audit --source "原文.docx" --translation "译文.docx" --target en --output "audit-result"
```

audit 只检查、不写原文件。Markdown/TXT 按可翻译片段顺序，Word 按部件和文本节点顺序对应。片段数量或结构不一致时输出对齐阻断，Agent 人工重建对应关系，不按相似文本猜测后继续报通过。

自动审校检查数值、代码、链接、占位符和传入的 confirmed 术语。报告包含原文/译文对照供 Agent 做语义审校；这是独立模式，脚本不会自动调用模型。否定、责任主体、条件、时间和单位含义必须人工或 Agent 逐项判断。语义审校记录另附 `semantic-review.md`，说明实际覆盖范围和待确认项。

## 交付状态

报告的机器检查结论和语义/版面结论独立呈现。`PASS_AUTOMATED` 只说明已执行的机械检查通过。`NEEDS_CONFIRMATION` 存在待确认译法或跳过内容；`FAIL` 有缺译、来源变化或结构/保护失败。版面未渲染和独立语义复核未做必须显示 `UNVERIFIED`。

`translation-report.html` 是离线报告，所有文档文本按普通文本转义。译文位于 `translated/`。原文和报告可能含敏感资料，用户未要求时不要上传第三方。

核对保留区确实应保持原样后，使用 `record-review --work "translation-work" --file "help.md" --scope format --status PASS --notes "实际检查范围与结果"` 记录；Word 渲染并逐页检查后用 `--scope layout` 记录。再次 build 刷新报告。记录绑定源文件与输出摘要，文件变化后自动失效。未实际检查不得写 PASS。

依赖：核心脚本仅需 Python 3.10+ 标准库。不自动安装软件。Word 可视化检查按运行环境的工具执行；缺失时明确说明安装用途与许可，不能伪造预览通过。
