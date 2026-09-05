"""etl/report.md generation — the human review surface (docs/SCHEMA.md section 7)."""
from __future__ import annotations

from .locate import LocationReport
from .parse import ParseResult
from .writeback import WritebackReport


def render_report(
    result: ParseResult,
    location: LocationReport | None = None,
    live: bool = False,
    writeback: WritebackReport | None = None,
) -> str:
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

        lines.append(
            f"### Plus codes: {location.plus_code_global} global (decoded offline, no call), "
            f"{location.plus_code_compound} compound (need geocoding)"
        )
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

        lines.append(f"### Maps link (long, no call needed) ({len(location.maps_link_long)})")
        lines.append("")
        if location.maps_link_long:
            for e in location.maps_link_long:
                lines.append(f"- {e}")
        else:
            lines.append("- none")
        lines.append("")

        lines.append(f"### Maps link (short, needs a redirect follow) ({len(location.maps_link_short)})")
        lines.append("")
        if location.maps_link_short:
            for e in location.maps_link_short:
                lines.append(f"- {e}")
        else:
            lines.append("- none")
        lines.append("")

        lines.append(
            f"### Still needs a geocode ({len(location.still_needs_geocode)}) — "
            "candidates for replacing with a Maps link instead"
        )
        lines.append("")
        if location.still_needs_geocode:
            for e in location.still_needs_geocode:
                lines.append(f"- {e}")
        else:
            lines.append("- none")
        lines.append("")

        lines.append(f"### Unparseable Maps URLs ({len(location.unparseable_maps_urls)})")
        lines.append("")
        if location.unparseable_maps_urls:
            for e in location.unparseable_maps_urls:
                lines.append(f"- {e}")
        else:
            lines.append("- none")
        lines.append("")

        lines.append(
            f"### APPROXIMATE precision ({len(location.approximate)}) — "
            "Google found no specific feature, guessed at an area"
        )
        lines.append("")
        if location.approximate:
            for e in location.approximate:
                lines.append(f"- {e}")
        else:
            lines.append("- none")
        lines.append("")

        lines.append(
            f"### GEOMETRIC_CENTER precision ({len(location.geometric_center)}) — "
            "center of a feature's bounds (e.g. a park or trail), not a pin"
        )
        lines.append("")
        if location.geometric_center:
            for e in location.geometric_center:
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

    if writeback is not None:
        lines.append("## Write-back")
        lines.append("")

        if writeback.aborted:
            lines.append(f"**Aborted:** {writeback.abort_reason}")
            lines.append("")

        lines.append(f"### Cells written: {writeback.cells_written}")
        lines.append("")

        lines.append(f"### Ids newly assigned ({len(writeback.ids_assigned)})")
        lines.append("")
        if writeback.ids_assigned:
            for a in writeback.ids_assigned:
                lines.append(f"- {a}")
        else:
            lines.append("- none")
        lines.append("")

        lines.append(f"### Unmatched rows ({len(writeback.unmatched)})")
        lines.append("")
        if writeback.unmatched:
            for u in writeback.unmatched:
                lines.append(f"- {u}")
        else:
            lines.append("- none")
        lines.append("")

        lines.append(f"### Would write (dry run / --no-writeback / preview) ({len(writeback.would_write)})")
        lines.append("")
        if writeback.would_write:
            for w in writeback.would_write:
                lines.append(f"- {w}")
        else:
            lines.append("- none")
        lines.append("")

    return "\n".join(lines) + "\n"
