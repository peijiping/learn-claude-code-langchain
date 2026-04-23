#!/usr/bin/env python3
"""
生成高血压诊断Markdown报告

将JSON诊断结果转换为格式化的Markdown文档
支持自定义模板和输出格式

用法:
    python generate_diagnosis_report.py <input_json> -o <output_md>
    python generate_diagnosis_report.py <input_json> --template <template_file> -o <output_md>
"""

import json
import argparse
from datetime import datetime
from pathlib import Path


def load_json(file_path):
    """加载JSON文件"""
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def format_risk_level(risk_level):
    """格式化风险等级显示"""
    risk_emojis = {"低危": "🟢", "中危": "🟡", "高危": "🟠", "很高危": "🔴"}
    emoji = risk_emojis.get(risk_level, "⚪")
    return f"{emoji} {risk_level}"


def format_grade(grade):
    """格式化高血压分级"""
    grade_emojis = {
        "1级": "🟡 1级（轻度）",
        "2级": "🟠 2级（中度）",
        "3级": "🔴 3级（重度）",
    }
    return grade_emojis.get(grade, grade)


def generate_markdown_report(diagnosis_result, template=None):
    """
    生成Markdown格式的诊断报告

    参数:
        diagnosis_result: JSON格式的诊断结果
        template: 可选的自定义模板

    返回:
        Markdown格式的报告字符串
    """

    if template:
        # 如果使用自定义模板，这里可以实现模板渲染逻辑
        pass

    # 提取关键信息
    status = diagnosis_result.get("diagnosis_status", "unknown")
    diagnosis = diagnosis_result.get("diagnosis", {})
    risk = diagnosis_result.get("risk_assessment", {})
    evidence = diagnosis_result.get("evidence_summary", [])
    missing = diagnosis_result.get("missing_data", {})

    # 开始生成报告
    report_lines = []

    # 报告标题
    report_lines.append("# 高血压诊断报告")
    report_lines.append("")
    report_lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append("")
    report_lines.append("---")
    report_lines.append("")

    # 诊断状态
    if status == "insufficient_data":
        report_lines.append("## ⚠️ 诊断状态：数据不足")
        report_lines.append("")
        report_lines.append("**无法完成诊断，缺少关键数据。**")
        report_lines.append("")

        if "missing_critical_fields" in diagnosis_result:
            report_lines.append("### 缺失的关键字段：")
            report_lines.append("")
            for field in diagnosis_result["missing_critical_fields"]:
                report_lines.append(f"- ❌ {field}")
            report_lines.append("")

        if "recommendation" in diagnosis_result:
            report_lines.append("### 建议：")
            report_lines.append(f"")
            report_lines.append(f"> {diagnosis_result['recommendation']}")
            report_lines.append("")

        # 添加安全声明
        report_lines.append("---")
        report_lines.append("")
        report_lines.append("## 声明")
        report_lines.append("")
        report_lines.append(
            diagnosis_result.get(
                "safety_notice",
                "本结论基于已提供的有限临床证据生成，不构成最终医疗决策。",
            )
        )
        report_lines.append("")

        return "\n".join(report_lines)

    # 诊断结论部分
    report_lines.append("## 📋 诊断结论")
    report_lines.append("")

    is_hypertension = diagnosis.get("is_hypertension", False)
    if is_hypertension:
        report_lines.append("✅ **高血压诊断：确诊**")
        report_lines.append("")
        report_lines.append(f"**类型**: {diagnosis.get('type', '原发性高血压')}")
        report_lines.append("")
        report_lines.append(f"**分级**: {format_grade(diagnosis.get('grade', '未知'))}")
    else:
        report_lines.append("❌ **高血压诊断：未达到诊断标准**")
    report_lines.append("")

    # 风险分层部分
    report_lines.append("---")
    report_lines.append("")
    report_lines.append("## ⚖️ 心血管风险分层")
    report_lines.append("")

    risk_level = risk.get("risk_level")
    if risk_level:
        report_lines.append(f"### {format_risk_level(risk_level)}")
        report_lines.append("")

        # 危险因素
        risk_count = risk.get("risk_factor_count", 0)
        risk_factors = risk.get("risk_factors", [])

        if risk_factors:
            report_lines.append(f"**危险因素数量**: {risk_count}个")
            report_lines.append("")
            report_lines.append("**存在的危险因素**:")
            report_lines.append("")
            for factor in risk_factors:
                report_lines.append(f"- ⚠️ {factor}")
            report_lines.append("")
        else:
            report_lines.append("**无明确心血管危险因素**")
            report_lines.append("")

        # 靶器官损害
        target_organ_damage = risk.get("target_organ_damage", [])
        if target_organ_damage:
            report_lines.append("**靶器官损害**:")
            report_lines.append("")
            for damage in target_organ_damage:
                report_lines.append(f"- 💔 {damage}")
            report_lines.append("")

        # 临床合并症
        clinical_complications = risk.get("clinical_complications", [])
        if clinical_complications:
            report_lines.append("**临床合并症**:")
            report_lines.append("")
            for comp in clinical_complications:
                report_lines.append(f"- 🏥 {comp}")
            report_lines.append("")
    else:
        report_lines.append("未达到高血压诊断标准，未进行风险分层。")
        if "note" in risk:
            report_lines.append("")
            report_lines.append(f"> 💡 {risk['note']}")
        report_lines.append("")

    # 证据摘要
    report_lines.append("---")
    report_lines.append("")
    report_lines.append("## 📊 诊断依据")
    report_lines.append("")

    if evidence:
        report_lines.append("**主要临床证据**:")
        report_lines.append("")
        for i, item in enumerate(evidence, 1):
            report_lines.append(f"{i}. {item}")
        report_lines.append("")

    # 缺失数据提示
    report_lines.append("---")
    report_lines.append("")
    report_lines.append("## ⚠️ 数据完整性提示")
    report_lines.append("")

    critical_missing = missing.get("critical", [])
    recommended_missing = missing.get("recommended", [])

    if critical_missing:
        report_lines.append("### 🔴 关键缺失数据（影响诊断）")
        report_lines.append("")
        for item in critical_missing:
            report_lines.append(f"- ❌ {item}")
        report_lines.append("")

    if recommended_missing:
        report_lines.append("### 🟡 推荐检查缺失（影响风险评估）")
        report_lines.append("")
        for item in recommended_missing:
            report_lines.append(f"- ⚠️ {item}")
        report_lines.append("")

    if not critical_missing and not recommended_missing:
        report_lines.append("✅ **数据完整，无重要缺失**")
        report_lines.append("")

    # 指南参考
    report_lines.append("---")
    report_lines.append("")
    report_lines.append("## 📚 诊断依据说明")
    report_lines.append("")

    guideline = diagnosis_result.get(
        "guideline_reference", "中国高血压防治指南（2011修订版）"
    )
    report_lines.append(f"**参考指南**: {guideline}")
    report_lines.append("")

    # 添加诊断逻辑说明
    report_lines.append("### 诊断标准")
    report_lines.append("")
    report_lines.append("根据《中国高血压防治指南（2011修订版）》：")
    report_lines.append("")
    report_lines.append(
        "1. **高血压诊断标准**：非同日3次测量，SBP≥140 mmHg和/或DBP≥90 mmHg"
    )
    report_lines.append("2. **高血压分级**：")
    report_lines.append("   - 1级：SBP 140-159 mmHg 和/或 DBP 90-99 mmHg")
    report_lines.append("   - 2级：SBP 160-179 mmHg 和/或 DBP 100-109 mmHg")
    report_lines.append("   - 3级：SBP≥180 mmHg 和/或 DBP≥110 mmHg")
    report_lines.append(
        "3. **心血管风险分层**：综合考虑血压水平、危险因素、靶器官损害和临床合并症"
    )
    report_lines.append("")

    # 安全声明
    report_lines.append("---")
    report_lines.append("")
    report_lines.append("## ⚠️ 重要声明")
    report_lines.append("")
    report_lines.append(
        "> **注意**: "
        + diagnosis_result.get(
            "safety_notice",
            "本结论基于已提供的有限临床证据生成，不构成最终医疗决策。所有诊断结论需经执业医师审核确认，患者应前往正规医疗机构就诊。",
        )
    )
    report_lines.append("")

    # 页脚
    report_lines.append("---")
    report_lines.append("")
    report_lines.append(f"*报告生成时间: {datetime.now().strftime('%Y年%m月%d日')}*")
    report_lines.append("")
    report_lines.append("*本报告仅供参考，不能替代专业医疗诊断*")
    report_lines.append("")

    return "\n".join(report_lines)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="将高血压诊断JSON结果转换为Markdown报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  python generate_diagnosis_report.py diagnosis_result.json -o report.md
  python generate_diagnosis_report.py diagnosis_result.json --template custom.md -o report.md
  python generate_diagnosis_report.py diagnosis_result.json --stdout
        """,
    )

    parser.add_argument("input_file", type=str, help="输入的JSON诊断结果文件路径")
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="输出的Markdown文件路径（默认：使用输入文件名）",
    )
    parser.add_argument(
        "--template", type=str, default=None, help="自定义Markdown模板文件路径（可选）"
    )
    parser.add_argument(
        "--stdout", action="store_true", help="输出到标准输出而不是文件"
    )
    parser.add_argument(
        "--encoding", type=str, default="utf-8", help="文件编码（默认：utf-8）"
    )

    args = parser.parse_args()

    # 检查输入文件
    input_path = Path(args.input_file)
    if not input_path.exists():
        print(f"错误: 输入文件不存在: {args.input_file}")
        return 1

    try:
        # 加载JSON数据
        diagnosis_result = load_json(args.input_file)

        # 生成Markdown报告
        markdown_report = generate_markdown_report(diagnosis_result, args.template)

        if args.stdout:
            # 输出到标准输出
            print(markdown_report)
        else:
            # 确定输出文件路径
            if args.output:
                output_path = Path(args.output)
            else:
                output_path = input_path.with_suffix(".md")

            # 写入文件
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding=args.encoding) as f:
                f.write(markdown_report)

            print(f"✅ 报告已生成: {output_path}")
            print(f"   文件大小: {output_path.stat().st_size} 字节")

    except json.JSONDecodeError as e:
        print(f"错误: JSON解析失败: {e}")
        return 1
    except Exception as e:
        print(f"错误: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())


# 示例JSON输入格式:
# {
#   "diagnosis_status": "completed",
#   "diagnosis": {
#     "is_hypertension": true,
#     "type": "原发性高血压",
#     "grade": "2级"
#   },
#   "risk_assessment": {
#     "risk_level": "高危",
#     "risk_factor_count": 3,
#     "risk_factors": ["吸烟", "糖尿病", "血脂异常"],
#     "target_organ_damage": ["肾脏损害"]
#   },
#   "evidence_summary": [
#     "多次诊室血压 158/96 mmHg",
#     "正在接受降压治疗"
#   ],
#   "missing_data": {
#     "critical": [],
#     "recommended": ["心电图", "超声心动图"]
#   },
#   "guideline_reference": "中国高血压防治指南（2011修订版）",
#   "safety_notice": "本结论基于已提供的有限临床证据生成，不构成最终医疗决策。"
# }
