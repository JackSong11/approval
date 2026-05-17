---
name: 532-payment-audit
description: 业务类型为532（聚合支付）的审核规则。
---

# 532-payment-audit Skill

## 概述
本技能用于自动化处理532业务类型（聚合支付）的审核规则。

## 工作流程
1. 从审核单据的`备注`字段中提取 鲸盘地址url 和 年份（从备注文本中提取，如"26年"表示2026年），注意参数只是简写的数字，比如24，25，26。 从`结算客商名称`字段获取结算客商名称。
2. 使用提取的信息作为参数，调用`${CLAUDE_SKILL_DIR}/scripts/jingpan_pdf_download.py`脚本下载合同PDF文件到本地目录，返回的本地PDF文件路径。
3. 将下载的本地PDF路径作为参数，调用`${CLAUDE_SKILL_DIR}/scripts/ocr_pdf.py`脚本，获取OCR提取的合同文本内容。
4. 将审核单据的字段、审核点要求与OCR提取的合同文本内容进行深度对比验证。
5. 生成审核结果，包括是否通过（isAccess）和审核意见（auditOpinion）。

## 调用示例 (Few-shot)

### 示例 1: 下载并审核 532 聚合支付单据
**输入信息**:
- 备注: "26年2月聚合支付新系统 , 附件链接：https://3.cn/10jhQqE-G"
- 结算客商名称: "河南某某公司"

**Agent 动作**:
# 第一步：下载（注意参数顺序：URL, 年份, 客商名）
python3 ${CLAUDE_SKILL_DIR}/scripts/jingpan_pdf_download.py "https://3.cn/10jhQqE-G" "26" "河南某某公司"

# 第二步：OCR（假设第一步返回了路径）
python3 ${CLAUDE_SKILL_DIR}/scripts/ocr_pdf.py "xxx/downloads/task_123/contract.pdf"

## 脚本依赖
本技能依赖以下两个脚本，位于`${CLAUDE_SKILL_DIR}/scripts/`目录下：
- `${CLAUDE_SKILL_DIR}/scripts/jingpan_pdf_download.py`：负责从鲸盘地址下载合同PDF文件
- `${CLAUDE_SKILL_DIR}/scripts/ocr_pdf.py`：负责对下载的PDF文件进行OCR识别，提取文本内容
