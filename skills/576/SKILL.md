---
name: 576
description: 业务类型为576的审核规则。
---

# 576 Skill

## 概述
本技能用于自动化处理576业务类型的审核规则。

## 工作流程
1. 从审核单据的`附件地址`字段中提取附件地址url。
2. 将提取附件地址url作为参数，调用`${CLAUDE_SKILL_DIR}/scripts/ocr_pdf.py`脚本，获取OCR提取的合同文本内容。
3. 将审核单据的字段、审核点要求与OCR提取的合同文本内容进行深度对比验证。
4. 生成审核结果，包括是否通过（isAccess）和审核意见（auditOpinion）。

## 脚本依赖
本技能依赖以下两个脚本，位于`${CLAUDE_SKILL_DIR}/scripts/`目录下：
- `${CLAUDE_SKILL_DIR}/scripts/ocr_pdf.py`：负责对PDF文件进行OCR识别，提取文本内容
