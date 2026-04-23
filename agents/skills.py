#!/usr/bin/env python3
"""
技能加载器模块

该模块实现了两层技能注入机制：
- 第一层：系统提示中仅包含技能名称和简短描述（低成本）
- 第二层：按需加载完整技能内容（tool_result中返回）

技能文件存放在 skills/<name>/SKILL.md 目录中，采用 YAML frontmatter 格式：
  ---
  name: skill-name
  description: 技能描述
  tags: tag1, tag2
  ---
  技能正文内容...
"""

import re
from pathlib import Path


class SkillLoader:
    """
    技能加载器

    扫描 skills/ 目录下的所有 SKILL.md 文件，
    解析其中的 YAML frontmatter 元数据，
    并提供两层访问接口：
    - get_descriptions(): 获取简短描述用于系统提示
    - get_content(): 获取完整内容用于按需加载
    """

    def __init__(self, skills_dir: Path):
        """
        初始化技能加载器

        Args:
            skills_dir: 技能目录路径（包含多个 <name>/SKILL.md 结构）
        """
        self.skills_dir = skills_dir      # 技能根目录
        self.skills = {}                  # 存储已加载的技能：{name: {meta, body, path}}
        self._load_all()                  # 启动时自动加载所有技能

    def _load_all(self):
        """
        递归加载所有技能文件

        遍历 skills_dir 目录下所有 SKILL.md 文件，
        解析 frontmatter 和正文内容，存入 self.skills 字典
        """
        # 如果目录不存在，直接返回
        if not self.skills_dir.exists():
            return

        # 递归查找所有 SKILL.md 文件（rglob 支持子目录）
        # sorted() 确保加载顺序稳定，便于调试和测试
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text()                      # 读取文件内容
            meta, body = self._parse_frontmatter(text) # 解析 frontmatter
            # 优先使用 frontmatter 中的 name，否则使用目录名
            name = meta.get("name", f.parent.name)
            # 存储技能信息：元数据、正文、文件路径
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}

    def _parse_frontmatter(self, text: str) -> tuple:
        """
        解析 YAML frontmatter

        SKILL.md 文件格式：
        ---
        name: pdf
        description: 处理PDF文件
        tags: document, pdf
        ---
        这里是技能正文内容...

        Args:
            text: SKILL.md 文件的完整文本内容

        Returns:
            tuple: (meta字典, body正文) 元组
        """
        # 正则匹配 --- 包裹的 YAML frontmatter
        # ^---    : 行首的 ---
        # (.*?)   : 非贪婪匹配 frontmatter 内容（捕获组1）
        # \n---   : 换行后的 ---
        # (.*)    : 剩余部分为正文（捕获组2）
        # re.DOTALL: 让 . 匹配换行符
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)

        # 如果没有找到 frontmatter 格式，整个文本作为正文返回
        if not match:
            return {}, text

        # 解析 frontmatter 的键值对
        meta = {}
        frontmatter_text = match.group(1).strip()  # 取 --- 之间的内容
        for line in frontmatter_text.splitlines():
            if ":" in line:  # 跳过不包含冒号的行
                key, val = line.split(":", 1)      # 只分割第一个冒号
                meta[key.strip()] = val.strip()     # 去除首尾空格

        # 返回元数据和正文（均去除首尾空白）
        return meta, match.group(2).strip()

    def get_descriptions(self) -> str:
        """
        获取所有技能的简短描述（第一层：系统提示用）

        格式化为缩进列表，供系统提示使用：
          - skill-name: 技能描述 [tags]

        Returns:
            str: 格式化的技能描述字符串
        """
        # 无可用技能时返回提示信息
        if not self.skills:
            return "(no skills available)"

        lines = []
        for name, skill in self.skills.items():
            # 从元数据获取描述，默认为 "No description"
            desc = skill["meta"].get("description", "No description")
            # 获取标签（可选）
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            # 如果有标签，附加到行尾
            if tags:
                line += f" [{tags}]"
            lines.append(line)

        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        """
        获取指定技能的完整内容（第二层：按需加载）

        当模型调用 load_skill("pdf") 时，返回完整技能正文，
        包装在 <skill> 标签中供模型解析。

        Args:
            name: 技能名称

        Returns:
            str: 包装在 <skill> 标签中的完整技能内容，
                 或错误信息（如果技能不存在）
        """
        skill = self.skills.get(name)

        # 技能不存在时返回错误信息和可用技能列表
        if not skill:
            available = ', '.join(self.skills.keys())
            return f"Error: Unknown skill '{name}'. Available: {available}"

        # 包装在 <skill> 标签中返回，便于模型识别和处理
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"
