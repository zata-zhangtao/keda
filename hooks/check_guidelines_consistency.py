#!/usr/bin/env python3
"""检查 AI 指导文件与统一规范源的一致性。"""

from __future__ import annotations

import sys
from pathlib import Path


class GuidelinesChecker:
    """统一规范源与入口适配层一致性检查器。"""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.adapter_files = {
            "agents": project_root / "AGENTS.md",
            "claude": project_root / "CLAUDE.md",
            "cursor": project_root / ".cursor" / "commands" / "cursor.md",
            "github": project_root / ".github" / "copilot-instructions.md",
        }
        self.hub_files = {
            "index": project_root / "docs" / "ai-standards" / "index.md",
            "architecture": project_root / "docs" / "ai-standards" / "architecture.md",
            "code_reuse": project_root / "docs" / "ai-standards" / "code-reuse.md",
            "naming": project_root / "docs" / "ai-standards" / "naming.md",
            "comments_docstrings": project_root
            / "docs"
            / "ai-standards"
            / "comments-docstrings.md",
            "documentation": project_root
            / "docs"
            / "ai-standards"
            / "documentation.md",
            "testing": project_root / "docs" / "ai-standards" / "testing.md",
            "tooling": project_root / "docs" / "ai-standards" / "tooling.md",
        }

    def _read_text(self, path: Path) -> str:
        """Read a text file using explicit UTF-8 encoding."""
        return path.read_text(encoding="utf-8")

    def check_files_exist(self) -> bool:
        """检查入口文件和规范源文件是否存在。"""
        missing_files: list[str] = []

        for name, path in {**self.adapter_files, **self.hub_files}.items():
            if not path.exists():
                missing_files.append(f"{name}: {path}")

        if missing_files:
            print("❌ 缺少以下文件:")
            for missing_file in missing_files:
                print(f"   {missing_file}")
            return False

        print("✅ 入口文件与统一规范源文件都存在")
        return True

    def check_hub_content(self) -> bool:
        """检查统一规范源是否覆盖核心主题。"""
        required_phrases = {
            "index": [
                "source of truth",
                "AGENTS.md",
                ".github/copilot-instructions.md",
                "code-reuse.md",
            ],
            "architecture": [
                "src/backend/api/",
                "src/backend/core/",
                "docs/architecture/system-design.md",
            ],
            "code_reuse": ["复用优先", "参数游行", "AI 编码自检清单"],
            "naming": ["Fully Qualified Naming", "SSA", "data"],
            "comments_docstrings": ["Google Style", 'encoding="utf-8"', "TODO"],
            "documentation": ["mkdocs.yml", "mkdocstrings", "UTF-8"],
            "testing": ["uv", "Playwright", "npm"],
            "tooling": ["uv", "just", "pre-commit"],
        }
        issues: list[str] = []

        for file_name, phrases in required_phrases.items():
            file_content = self._read_text(self.hub_files[file_name])
            missing_phrases = [
                phrase for phrase in phrases if phrase not in file_content
            ]
            if missing_phrases:
                issues.append(f"{file_name} 缺少关键短语: {', '.join(missing_phrases)}")

        if issues:
            print("❌ 统一规范源内容不完整:")
            for issue in issues:
                print(f"   {issue}")
            return False

        print("✅ 统一规范源覆盖了核心主题")
        return True

    def check_adapter_references(self) -> bool:
        """检查入口适配层是否指向统一规范源。"""
        required_references = {
            "agents": [
                "docs/ai-standards/index.md",
                "docs/architecture/system-design.md",
            ],
            "claude": ["docs/ai-standards/index.md", "AGENTS.md"],
            "cursor": ["docs/ai-standards/index.md", "AGENTS.md"],
            "github": ["docs/ai-standards/", ".github/instructions/"],
        }
        issues: list[str] = []

        for file_name, references in required_references.items():
            file_content = self._read_text(self.adapter_files[file_name])
            missing_references = [
                reference for reference in references if reference not in file_content
            ]
            if missing_references:
                issues.append(f"{file_name} 缺少引用: {', '.join(missing_references)}")

        if issues:
            print("❌ 入口适配层引用不完整:")
            for issue in issues:
                print(f"   {issue}")
            return False

        print("✅ 入口适配层都正确指向统一规范源")
        return True

    def check_source_of_truth_language(self) -> bool:
        """检查入口适配层没有重新声称自己是唯一权威来源。"""
        disallowed_phrases = [
            "complete and authoritative specifications",
            "完整且权威的主规范",
            "唯一权威来源",
        ]
        issues: list[str] = []

        for file_name, file_path in self.adapter_files.items():
            file_content = self._read_text(file_path)
            matched_phrases = [
                phrase for phrase in disallowed_phrases if phrase in file_content
            ]
            if matched_phrases:
                issues.append(
                    f"{file_name} 仍包含旧的单点权威表述: {', '.join(matched_phrases)}"
                )

        if issues:
            print("❌ 入口适配层仍存在单点权威表述:")
            for issue in issues:
                print(f"   {issue}")
            return False

        print("✅ 入口适配层未重新声明自己是唯一权威来源")
        return True

    def run_all_checks(self) -> bool:
        """运行所有一致性检查。"""
        print("🔍 检查统一规范源与入口适配层一致性...\n")

        checks = [
            self.check_files_exist,
            self.check_hub_content,
            self.check_adapter_references,
            self.check_source_of_truth_language,
        ]

        all_passed = True
        for check in checks:
            if not check():
                all_passed = False
            print()

        if all_passed:
            print("🎉 所有检查通过！统一规范源与入口适配层保持一致。")
        else:
            print("⚠️  发现一致性问题，请根据上述报告修复。")

        return all_passed


def main() -> None:
    """运行入口。"""
    project_root = Path(__file__).parent.parent
    checker = GuidelinesChecker(project_root)
    success = checker.run_all_checks()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
