"""Tests for backend.core.shared.prd_checklist."""

from __future__ import annotations


from backend.core.shared.prd_checklist import PrdChecklistResult, parse_prd_checklist


class TestParsePrdChecklist:
    """Tests for parse_prd_checklist."""

    def test_empty_file_returns_no_section(self) -> None:
        """Empty content should report section not found."""
        result = parse_prd_checklist("")
        assert result == PrdChecklistResult(section_found=False, unchecked_items=[])

    def test_no_acceptance_section_returns_not_found(self) -> None:
        """Content without acceptance heading should report section not found."""
        content = "# PRD\n\n## Some Other Section\n\n- [ ] item\n"
        result = parse_prd_checklist(content)
        assert result == PrdChecklistResult(section_found=False, unchecked_items=[])

    def test_all_checked_items_are_complete(self) -> None:
        """When all items are checked, unchecked_items should be empty."""
        content = "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] item 1",
                "- [X] item 2",
                "",
            ]
        )
        result = parse_prd_checklist(content)
        assert result.section_found is True
        assert result.unchecked_items == []
        assert result.is_complete is True

    def test_unchecked_items_are_reported_with_line_numbers(self) -> None:
        """Unchecked items should include 1-based line numbers."""
        content = "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] done",
                "- [ ] undone 1",
                "- [ ] undone 2",
                "",
            ]
        )
        result = parse_prd_checklist(content)
        assert result.section_found is True
        assert result.unchecked_items == [
            (6, "- [ ] undone 1"),
            (7, "- [ ] undone 2"),
        ]
        assert result.is_complete is False

    def test_checkboxes_outside_section_are_ignored(self) -> None:
        """Checkboxes before or after the acceptance section should be ignored."""
        content = "\n".join(
            [
                "# PRD",
                "",
                "- [ ] before section",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] done",
                "",
                "## Notes",
                "",
                "- [ ] after section",
            ]
        )
        result = parse_prd_checklist(content)
        assert result.section_found is True
        assert result.unchecked_items == []

    def test_checkboxes_inside_code_blocks_are_ignored(self) -> None:
        """Checkboxes inside fenced code blocks within the section should be ignored."""
        content = "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [x] real item",
                "",
                "```markdown",
                "- [ ] inside code block",
                "```",
                "",
                "- [ ] real unchecked",
            ]
        )
        result = parse_prd_checklist(content)
        assert result.unchecked_items == [(11, "- [ ] real unchecked")]

    def test_numbered_heading_is_recognized(self) -> None:
        """Numbered headings like '## 7. Acceptance Checklist' should match."""
        content = "\n".join(
            [
                "# PRD",
                "",
                "## 7. Acceptance Checklist",
                "",
                "- [x] done",
                "- [ ] undone",
                "",
            ]
        )
        result = parse_prd_checklist(content)
        assert result.section_found is True
        assert result.unchecked_items == [(6, "- [ ] undone")]

    def test_bilingual_heading_is_recognized(self) -> None:
        """Bilingual headings like 'Acceptance Checklist（验收清单）' should match."""
        content = "\n".join(
            [
                "# PRD",
                "",
                "## 7. Acceptance Checklist（验收清单）",
                "",
                "- [x] done",
                "- [ ] undone",
                "",
            ]
        )
        result = parse_prd_checklist(content)
        assert result.section_found is True
        assert result.unchecked_items == [(6, "- [ ] undone")]

    def test_section_ends_at_next_top_level_heading(self) -> None:
        """The acceptance section should stop at the next '##' heading."""
        content = "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "- [ ] first",
                "",
                "## Next Section",
                "",
                "- [ ] second",
            ]
        )
        result = parse_prd_checklist(content)
        assert result.unchecked_items == [(5, "- [ ] first")]

    def test_empty_checklist_section_is_complete(self) -> None:
        """A checklist section with no checkboxes at all should be considered complete."""
        content = "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "Some text without checkboxes.",
                "",
            ]
        )
        result = parse_prd_checklist(content)
        assert result.section_found is True
        assert result.unchecked_items == []
        assert result.is_complete is True

    def test_tilde_code_fences_are_ignored(self) -> None:
        """Tilde-style fenced code blocks should also be ignored."""
        content = "\n".join(
            [
                "# PRD",
                "",
                "## Acceptance Checklist",
                "",
                "~~~python",
                "- [ ] inside tilde block",
                "~~~",
                "",
                "- [x] done",
            ]
        )
        result = parse_prd_checklist(content)
        assert result.unchecked_items == []
