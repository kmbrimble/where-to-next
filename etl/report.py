"""etl/report.md generation — the human review surface (docs/SCHEMA.md section 7)."""
from __future__ import annotations

from .parse import ParseResult


def render_report(result: ParseResult) -> str:
    lines = ["# ETL Report", ""]

    lines.append("## Rows parsed by type")
    lines.append("")
    if result.counts:
        for row_type in sorted(result.counts):
            lines.append(f"- {row_type}: {result.counts[row_type]}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append(f"## Rows skipped ({result.skipped})")
    lines.append("")
    lines.append("Rows with an empty row_type — not yet migrated, not an error.")
    lines.append("")

    lines.append(f"## Errors ({len(result.errors)})")
    lines.append("")
    if result.errors:
        for e in result.errors:
            lines.append(f"- {e}")
    else:
        lines.append("- none")
    lines.append("")

    lines.append(f"## Warnings ({len(result.warnings)})")
    lines.append("")
    if result.warnings:
        for w in result.warnings:
            lines.append(f"- {w}")
    else:
        lines.append("- none")
    lines.append("")

    return "\n".join(lines) + "\n"
