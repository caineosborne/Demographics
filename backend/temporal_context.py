"""Runtime date and vintage guidance shared by the demographics agents."""

from datetime import date


def temporal_context() -> str:
    """Evaluate the date at invocation time, including in long-running servers."""
    return f"""Today's date is {date.today().isoformat()} (YYYY-MM-DD).
Treat this as the authoritative current date for this analysis.
The local UN World Population Prospects dataset is the 2024 revision, created
in 2024. Its vintage/effective as-of year is 2024; it does not incorporate later
revisions or observations. This is the baseline's vintage, not today's date
and not the last year for which it provides projections.
In the local database, historical estimates cover 1950–2023; medium-variant
projections cover 2024–2100. Use historic=True only through 2023 and
historic=False for 2024 onward. A 2026 UN figure is a projection made in the
2024 revision, even when comparing it with subsequently reported 2026 actuals.
Judge whether a source's reporting period is future-dated relative to today's
date, never relative to the UN vintage or an assumed collection cycle. A period
that has already ended is not future-dated merely because it is after 2024.
Do not infer a publication date from the reporting period or dataset vintage.

"""
