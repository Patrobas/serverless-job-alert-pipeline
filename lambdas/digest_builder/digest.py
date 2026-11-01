"""Builds the plain-text email body. Kept separate from the Lambda handler
so it can be tested without AWS."""

from datetime import datetime, timezone


def build_digest(jobs, total_matches):
    """jobs: already sorted, best first. Returns (subject, body)."""
    today = datetime.now(timezone.utc).strftime("%b %d")
    subject = f"Job digest {today}: {len(jobs)} top matches"

    lines = [f"{len(jobs)} best matches out of {total_matches} waiting.", ""]
    for rank, job in enumerate(jobs, start=1):
        lines.append(f"{rank}. [{job['score']}] {job['title']} at {job['company']}")
        lines.append(f"   {job.get('location') or 'Location not listed'}")
        also = job.get("also_in") or []
        if also:
            shown = "; ".join(also[:5]) + (f"; and {len(also) - 5} more" if len(also) > 5 else "")
            lines.append(f"   Same role also listed in: {shown}")
        lines.append(f"   Why: {job.get('reason', '')}")
        read_url = job.get("url") or job.get("apply_url")
        apply_url = job.get("apply_url") or read_url
        lines.append(f"   Read the job: {read_url}")
        if apply_url != read_url:  # many platforms use a separate page for the application form
            lines.append(f"   Apply: {apply_url}")
        lines.append("")

    if any(job.get("source") == "remoteok" for job in jobs):
        lines.append("Some listings come from RemoteOK (https://remoteok.com).")
    return subject[:100], "\n".join(lines)
