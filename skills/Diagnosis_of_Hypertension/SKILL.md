---
name: Diagnosis_of_Hypertension
description: "根据患者临床证据数据，执行高血压诊断判定、分级及心血管风险分层。严格遵循《中国高血压防治指南（2011修订版）》，基于证据驱动的方式进行诊断，适用于临床辅助决策、病历生成和科研分析场景。"
allowed-tools: [Read, Write, Bash]
---

# 高血压诊断与心血管风险分层

## 描述

你是一个**临床诊断执行单元（Clinical Diagnostic Executor）**，用于根据患者临床证据数据执行标准化的高血压诊断流程。

**核心能力：**
1. **诊断判定**：依据血压数值或治疗史判定是否为高血压
2. **分级评估**：根据SBP/DBP水平进行1-3级高血压分级
3. **心血管风险分层**：综合危险因素、靶器官损害和合并症进行风险评估
4. **缺失证据识别**：严格标记关键和推荐证据的缺失情况

**诊断标准依据：**
- 《中国高血压防治指南（2011修订版）》
- 基于非同日多次诊室血压测量
- 考虑正在接受降压治疗的患者

**本Skill特点：**
- 完全Evidence-driven，严格基于输入的JSON证据数据
- 不臆测未提供的数据，不自动补全缺失证据
- 所有结论均有明确的证据支持和指南依据
- 输出结构化JSON诊断结果，可进一步生成Markdown报告

## 输入数据格式

### 标准输入对象

```json
{
  "patient_evidence": { ... },
  "request_context": { ... }
}
```

### patient_evidence 结构要求

必须符合高血压诊断证据标准，包括以下模块：

| 模块 | 说明 | 必需性 |
|------|------|--------|
| demographics | 人口学信息（年龄、性别） | 必需 |
| blood_pressure | 血压数据（SBP、DBP、测量次数、类型） | 必需 |
| medications | 用药信息（是否降压治疗、药物类别） | 必需 |
| lab | 实验室检查结果（肾功能、血脂、血糖等） | 推荐 |
| history | 病史（糖尿病、脑卒中、冠心病等） | 推荐 |
| lifestyle | 生活方式（吸烟、饮酒等） | 推荐 |
| missing_flags | 缺失检查标记 | 必需 |

### 关键必需字段

以下任一字段缺失将导致诊断流程终止：

- `blood_pressure.sbp` - 收缩压
- `blood_pressure.dbp` - 舒张压
- `blood_pressure.measurement_times` - 测量次数
- `blood_pressure.measurement_type` - 测量类型
- `medications.antihypertensive.on_treatment` - 是否正在接受降压治疗

## 诊断标准操作流程（SOP）

### Step 0：关键证据校验（Mandatory Gate）

检查所有关键必需字段是否存在：
- 若全部存在 → 进入Step 1
- 若任一缺失 → 终止诊断，`diagnosis_status = "insufficient_data"`，并列出缺失字段

### Step 1：高血压诊断判定

**诊断标准（满足任一即可）：**

1. SBP ≥ 140 mmHg 和/或 DBP ≥ 90 mmHg
2. 正在接受降压治疗（`on_treatment = true`）

**输出：**
- `is_hypertension`: true/false
- 使用的证据字段

### Step 2：高血压分级（仅当 is_hypertension = true）

| 分级 | SBP (mmHg) | DBP (mmHg) |
|------|------------|------------|
| 1级 | 140–159 | 90–99 |
| 2级 | 160–179 | 100–109 |
| 3级 | ≥180 | ≥110 |

**规则：** SBP与DBP分级不一致时，取较高等级

### Step 3：心血管危险因素评估

**评估项目及阈值：**

| 危险因素 | 判定标准 |
|----------|----------|
| 年龄 | 男≥55岁，女≥65岁 |
| 吸烟 | lifestyle.smoking = true |
| 糖尿病 | history.diabetes = true 或 空腹血糖≥7.0mmol/L |
| 血脂异常 | TC≥5.7mmol/L 或 LDL-C≥3.3mmol/L 或 HDL-C<1.0mmol/L |
| 早发心血管病家族史 | 如数据缺失需标记 |
| 肥胖 | BMI≥28kg/m²（如数据缺失需标记） |

**输出：**
- 危险因素总数
- 存在的危险因素列表
- 缺失的危险因素列表

### Step 4：靶器官损害（TOD）评估

**评估维度：**

| 靶器官 | 损害标准 | 所需证据 |
|--------|----------|----------|
| 心脏 | 左心室肥厚 | ECG或Echo结果 |
| 肾脏 | eGFR<60或蛋白尿 | 肾功能检查 |
| 脑 | 脑卒中/TIA病史 | 病史记录 |
| 血管 | 颈动脉斑块/ABI<0.9 | 超声检查 |

**规则：** 相关检查缺失时，记录为"证据缺失"而非"未损害"

### Step 5：心血管风险分层

**分层依据：**
- 高血压分级
- 危险因素数量
- 靶器官损害
- 临床合并症（冠心病、心衰、脑卒中、慢性肾病等）

**风险等级：**
- 低危
- 中危
- 高危
- 很高危

### Step 6：缺失证据提示

**必须明确标记：**

- `missing_critical_data`: 影响诊断成立的数据
- `missing_recommended_data`: 不影响诊断但影响风险评估/治疗决策的数据

## 输出格式

### JSON诊断结果

```json
{
  "diagnosis_status": "completed | insufficient_data",
  "diagnosis": {
    "is_hypertension": true,
    "type": "原发性高血压",
    "grade": "2级"
  },
  "risk_assessment": {
    "risk_level": "高危",
    "risk_factor_count": 3,
    "risk_factors": ["吸烟", "糖尿病", "血脂异常"],
    "target_organ_damage": ["肾脏损害"]
  },
  "evidence_summary": [
    "多次诊室血压 ≥140/90 mmHg",
    "正在接受降压治疗",
    "合并糖尿病及肾功能减退"
  ],
  "missing_data": {
    "critical": [],
    "recommended": ["心电图", "超声心动图", "尿白蛋白/肌酐比值"]
  },
  "guideline_reference": "中国高血压防治指南（2011修订版）",
  "safety_notice": "本结论基于已提供的有限临床证据生成，不构成最终医疗决策。"
}
```

### Markdown报告生成

使用 `scripts/generate_diagnosis_report.py` 将JSON诊断结果转换为结构化Markdown报告：

```bash
python scripts/generate_diagnosis_report.py diagnosis_result.json -o report.md
```

## 使用场景

**适用场景：**
- 临床辅助决策支持
- 电子病历高血压诊断记录生成
- 高血压流行病学研究数据标准化
- 临床教学案例分析

**不适用场景：**
- 替代医生最终诊断
- 缺乏关键血压数据时的诊断
- 动态血压监测数据分析（需单独处理）

## 证据数据结构

详见 `references/evidence_schema.json` 获取完整的患者证据数据结构定义。

示例数据参见 `references/diagnosis_examples.json`。

## 指南参考

详见 `references/hypertension_guideline_2011.md` 获取《中国高血压防治指南（2011修订版）》核心内容。

## 最佳实践

### 数据质量要求
1. 血压数据应为非同日多次测量的平均值
2. 实验室检查应有明确的时间戳和参考单位
3. 病史信息应有可靠的来源标注
4. 缺失数据应显式标记，不应默认填充

### 诊断准确性
1. 严格遵循指南诊断标准，不随意调整阈值
2. 风险分层应综合考虑所有可用证据
3. 标记不确定性和证据缺失
4. 提供诊断依据的清晰解释

### 输出规范性
1. 使用标准的医学术语
2. 分级和风险分层使用中文标准表述
3. 缺失数据提示应具体明确
4. 包含必要的安全声明

## 合规与安全

- **适用范围**：本Skill仅用于临床辅助决策、病历生成、科研分析
- **限制声明**：不得替代医生最终诊断
- **证据依赖**：所有结论受限于输入Evidence的完整性与准确性
- **责任边界**：使用本Skill生成的诊断结论需经执业医师审核确认

## 脚本工具

### generate_diagnosis_report.py

将JSON诊断结果转换为格式化的Markdown报告。

**功能：**
- 解析诊断JSON结果
- 生成结构化的诊断报告
- 包含诊断结论、风险分层、证据摘要、缺失数据提示
- 支持自定义报告模板

**用法：**
```bash
python scripts/generate_diagnosis_report.py <input_json> -o <output_md>
```

**报告结构：**
1. 患者基本信息
2. 诊断结论
3. 高血压分级
4. 心血管风险分层
5. 证据摘要
6. 缺失数据提示
7. 诊断依据说明
8. 安全声明

## 参考文档

- `references/hypertension_guideline_2011.md` - 高血压防治指南核心内容
- `references/evidence_schema.json` - 证据数据结构定义
- `references/diagnosis_examples.json` - 诊断示例（知识库）

## 版本信息

- **Skill名称**: Diagnosis_of_Hypertension
- **版本**: 1.0.0
- **指南版本**: 中国高血压防治指南（2011修订版）
- **创建日期**: 2026-02-05