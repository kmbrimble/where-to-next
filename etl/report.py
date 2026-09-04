"""etl/report.md generation — the human review surface (docs/SCHEMA.md section 7)."""
from __future__ import annotations

from .locate import LocationReport
from .parse import ParseResult


def render_report(result: ParseResult, location: LocationReport | None = None, live: bool = False) -> str:
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

    if location is not None:
        lines.append("## Location")
        lines.append("")
        lines.append("### Counts by resolved_from")
        lines.append("")
        for key in sorted(location.counts):
            lines.append(f"- {key}: {location.counts[key]}")
        lines.append("")

        if live:
            lines.append(f"### Geocoding calls made: {location.actual_calls}")
        else:
            lines.append(f"### Geocoding calls projected (dry run, none made): {location.projected_calls}")
        lines.append("")

        lines.append(f"### Needs eyeballing ({len(location.eyeball)})")
        lines.append("")
        if location.eyeball:
            for e in location.eyeball:
                lines.append(f"- {e}")
        else:
            lines.append("- none")
        lines.append("")

        header = "### Would write (dry run)" if not live else "### Written"
        lines.append(f"{header} ({len(location.would_write)})")
        lines.append("")
        if location.would_write:
            for w in location.would_write:
                lines.append(f"- {w}")
        else:
            lines.append("- none")
        lines.append("")

    return "\n".join(lines) + "\n"
