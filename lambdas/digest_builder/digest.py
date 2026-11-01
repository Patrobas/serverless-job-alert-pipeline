"""Builds the plain-text email body. Kept separate from the Lambda handler
so it can be tested without AWS."""

from datetime import datetime, timezone


def _details(job):
    years = job.get("years_required")
    salary = job.get("salary_max")
    parts = [
        f"Work type: {job.get('work_type') or 'Not listed'}",
        f"Years required: {int(years)}" if years is not None else "Years required: not listed",
        f"Salary up to: ${int(salary):,}" if salary is not None else "Salary: not listed",
    ]
    return " | ".join(parts)


def build_digest(jobs, total_matches):
    """jobs: already sorted, best first. Returns (subject, body)."""
    today = datetime.now(timezone.utc).strftime("%b %d")
    subject = f"Patrobas Personalized AWS Job Digest, {today}: {len(jobs)} top matches"

    lines = [f"{len(jobs)} best matches out of {total_matches} waiting.", ""]
    for rank, job in enumerate(jobs, start=1):
        lines.append(f"{rank}. [{job['score']}] {job['title']} at {job['company']}")
        lines.append(f"   {job.get('location') or 'Location not listed'}")
        also = job.get("also_in") or []
        if also:
            shown = "; ".join(also[:5]) + (f"; and {len(also) - 5} more" if len(also) > 5 else "")
            lines.append(f"   Same role also listed in: {shown}")
        lines.append(f"   Why: {job.get('reason', '')}")
        red = job.get("red_flags") or []
        lines.append(f"   Red flags: {', '.join(red) if red else 'none found'}")
        lines.append("   " + _details(job))
        if job.get("low_confidence"):
            lines.append("   Low confidence: the full description was missing or short, so treat this score as a guess.")
        read_url = job.get("url") or job.get("apply_url")
        apply_url = job.get("apply_url") or read_url
        lines.append(f"   Read the job: {read_url}")
        if apply_url != read_url:  # many platforms use a separate page for the application form
            lines.append(f"   Apply: {apply_url}")
        lines.append("")

    if any(job.get("source") == "remoteok" for job in jobs):
        lines.append("Some listings come from RemoteOK (https://remoteok.com).")
    return subject[:100], "\n".join(lines)
