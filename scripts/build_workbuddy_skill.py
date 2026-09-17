"""把 afan 口播技能打包成 WorkBuddy 开放平台（open.workbuddy.cn）的技能 zip。

产物结构遵循平台技能规范：SKILL.md（frontmatter 含 name/description_zh 等必填字段）、
references/（AI 执行时阅读的参考文档）、scripts/（AI 通过 Bash 执行的脚本）。
所有内容都从仓库单一源组装：skills/workbuddy/SKILL.md、docs/agent-api.md、
scripts/afan_agent_cli.py，避免出现第二份需要同步的副本。

用法：
    python scripts/build_workbuddy_skill.py             # 输出到 work/workbuddy-skill/
    python scripts/build_workbuddy_skill.py --output D:\\tmp
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_NAME = "afan-talking-video"

# WorkBuddy 技能 frontmatter 的必填字段（见 open.workbuddy.cn/docs/skill）。
REQUIRED_FRONTMATTER = (
    "name",
    "display_name",
    "display_name_en",
    "description",
    "description_zh",
    "description_en",
    "category",
    "version",
    "author",
)


def _validate_frontmatter(skill_md: Path) -> dict[str, str]:
    content = skill_md.read_text(encoding="utf-8").lstrip("\ufeff")
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", content, re.DOTALL)
    if not match:
        raise SystemExit(f"{skill_md} 缺少 frontmatter（--- 包裹的 YAML 头）。")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    missing = [key for key in REQUIRED_FRONTMATTER if not fields.get(key)]
    if missing:
        raise SystemExit(f"{skill_md} frontmatter 缺少必填字段：{', '.join(missing)}")
    return fields


def build(output_dir: Path) -> Path:
    skill_md = ROOT / "skills" / "workbuddy" / "SKILL.md"
    fields = _validate_frontmatter(skill_md)

    stage = output_dir / SKILL_NAME
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "scripts").mkdir(parents=True)
    (stage / "references").mkdir()
    shutil.copyfile(skill_md, stage / "SKILL.md")
    shutil.copyfile(ROOT / "docs" / "agent-api.md", stage / "references" / "agent-api.md")
    shutil.copyfile(ROOT / "scripts" / "afan_agent_cli.py", stage / "scripts" / "afan_agent_cli.py")

    zip_path = output_dir / f"{SKILL_NAME}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(output_dir))

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    print(f"技能 zip：{zip_path}")
    print(f"SHA256：{digest}")
    print(f"大小：{zip_path.stat().st_size / 1024:.1f} KB")
    print(f"技能名：{fields['name']}  版本：{fields['version']}  分类：{fields['category']}")
    print("提醒：category 需为平台技能分类之一，上传时如平台校验不通过请按平台分类列表调整 skills/workbuddy/SKILL.md 后重新打包。")
    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 WorkBuddy 技能 zip")
    parser.add_argument("--output", type=Path, default=ROOT / "work" / "workbuddy-skill", help="产物输出目录（默认 work/workbuddy-skill，不入 Git）")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    build(args.output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
