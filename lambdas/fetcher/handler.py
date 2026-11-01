"""
Fetcher Lambda - Serverless Job Alert Pipeline

Runs on a schedule (EventBridge). For each source in SOURCES it:
  1. pulls the open postings from that source's public JSON API
  2. converts them to one common shape (normalize)
  3. drops obvious mismatches with a cheap filter (filters.py + profile.json)
  4. saves each survivor to DynamoDB, remembering which ones are new

Dedup uses a DynamoDB conditional write: PutItem succeeds only if that job
does not exist yet. One call both checks and writes, so two runs can never
both think the same job is new (no read-then-write race).

If a job already exists, we push its expiry date forward instead of ignoring
it. Without that, a posting open longer than the TTL would be deleted, look
brand new on the next run, and trigger a repeat alert.
"""

import hashlib
import html
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from filters import passes_filters

# How long a job stays in the table after we last saw it open.
TTL_DAYS = 45

# Full job descriptions are kept so the Matcher can read the requirements
# (years of experience, time zone, hybrid rules). Cut off at a sensible length
# so one huge posting cannot bloat the table.
MAX_DESCRIPTION_CHARS = 20000


def clean_text(raw, limit=MAX_DESCRIPTION_CHARS):
    """Turn the HTML (sometimes escaped HTML) a job board sends into plain text."""
    if not raw:
        return ""
    text = html.unescape(str(raw))
    text = re.sub(r"<(script|style).*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]

# ---------------------------------------------------------------------
# Sources. "ats" picks which fetch function to use, "slug" is the
# company's id on that platform. Slugs are unverified until the first
# real run; a wrong slug just shows up as an error for that one source.
# ---------------------------------------------------------------------
SOURCES = [
    {"name": "Anthropic", "ats": "greenhouse", "slug": "anthropic"},
    {"name": "Stripe", "ats": "greenhouse", "slug": "stripe"},
    {"name": "Veeva", "ats": "lever", "slug": "veeva"},
    {"name": "HEALWELL AI", "ats": "workable", "slug": "healwell-ai"},
    # Verified live on 2026-10-31 (each returned HTTP 200 with open jobs).
    {"name": "Ashby", "ats": "ashby", "slug": "ashby"},
    {"name": "Oyster", "ats": "ashby", "slug": "oyster"},
    {"name": "Remote", "ats": "greenhouse", "slug": "remote"},
    {"name": "Harvey", "ats": "ashby", "slug": "harvey"},
    {"name": "Smarsh", "ats": "lever", "slug": "smarsh"},
    {"name": "New Relic", "ats": "greenhouse", "slug": "newrelic"},
    {"name": "Wealthsimple", "ats": "ashby", "slug": "wealthsimple"},
    {"name": "Jobber", "ats": "ashby", "slug": "jobber"},
    {"name": "Hootsuite", "ats": "greenhouse", "slug": "hootsuite"},
    {"name": "1Password", "ats": "ashby", "slug": "1password"},
    {"name": "Procurify", "ats": "ashby", "slug": "procurify"},
    {"name": "Visier", "ats": "ashby", "slug": "visier"},
    {"name": "Cohere", "ats": "ashby", "slug": "cohere"},
    {"name": "Thinkific", "ats": "ashby", "slug": "thinkific"},
    {"name": "Dialpad", "ats": "greenhouse", "slug": "dialpad"},
    {"name": "Lightspeed", "ats": "ashby", "slug": "lightspeedhq"},
    {"name": "D2L", "ats": "greenhouse", "slug": "d2l"},
    {"name": "Benevity", "ats": "ashby", "slug": "benevity"},
    {"name": "Neo Financial", "ats": "ashby", "slug": "neofinancial"},
    {"name": "RemoteOK", "ats": "remoteok", "slug": "remoteok"},
]

_table = None


def get_table():
    """Create the DynamoDB handle on first use, not at import time.
    Keeps the module importable in tests without AWS credentials."""
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def lambda_handler(event, context):
    summary = {"fetched": 0, "kept": 0, "new": 0, "seen": 0}
    errors = []

    for source in SOURCES:
        try:
            postings = fetch_source(source)
            summary["fetched"] += len(postings)
            for posting in postings:
                if not passes_filters(posting):
                    continue
                summary["kept"] += 1
                summary[save_posting(posting)] += 1
        except Exception as exc:  # one broken source must not stop the rest
            errors.append(f"{source['name']}: {exc}")

    result = {"sources": len(SOURCES), **summary, "errors": errors}
    print(json.dumps(result))

    # If every source failed, something systemic is wrong (network, permissions).
    # Raising makes Lambda record an error, which is what the CloudWatch alarm watches.
    if len(errors) == len(SOURCES):
        raise RuntimeError(f"All sources failed: {errors}")
    return result


# ---------------------------------------------------------------------
# Fetching. One function per platform; each returns normalized postings.
# ---------------------------------------------------------------------
def fetch_source(source):
    fetchers = {
        "greenhouse": fetch_greenhouse,
        "lever": fetch_lever,
        "ashby": fetch_ashby,
        "workable": fetch_workable,
        "remoteok": fetch_remoteok,
    }
    if source["ats"] not in fetchers:
        raise ValueError(f"Unknown ATS type: {source['ats']}")
    return fetchers[source["ats"]](source)


def http_get_json(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "job-alert-pipeline", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def fetch_greenhouse(source):
    data = http_get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{source['slug']}/jobs?content=true"
    )
    return [
        normalize(
            source=source["slug"],
            company=source["name"],
            raw_id=str(job.get("id")),
            title=job.get("title"),
            location=(job.get("location") or {}).get("name"),
            url=job.get("absolute_url"),
            apply_url=job.get("absolute_url"),
            posted_at=job.get("updated_at"),
            description=clean_text(job.get("content")),
        )
        for job in data.get("jobs", [])
    ]


def fetch_lever(source):
    data = http_get_json(f"https://api.lever.co/v0/postings/{source['slug']}?mode=json")
    postings = []
    for job in data:
        categories = job.get("categories") or {}
        location = categories.get("location") or ""
        if job.get("workplaceType") == "remote" and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()
        sections = [job.get("descriptionPlain") or clean_text(job.get("description"))]
        for block in job.get("lists") or []:
            sections.append(f"{block.get('text', '')}\n{clean_text(block.get('content'))}")
        sections.append(job.get("additionalPlain") or clean_text(job.get("additional")))
        postings.append(normalize(
            source=source["slug"],
            company=source["name"],
            raw_id=job.get("id"),
            title=job.get("text"),
            location=location,
            url=job.get("hostedUrl"),
            apply_url=job.get("applyUrl") or job.get("hostedUrl"),
            posted_at=job.get("createdAt"),
            description=clean_text("\n".join(x for x in sections if x)),
        ))
    return postings


def fetch_ashby(source):
    data = http_get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{source['slug']}?includeCompensation=false"
    )
    postings = []
    for job in data.get("jobs", []):
        location = job.get("location") or ""
        if job.get("isRemote") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()
        postings.append(normalize(
            source=source["slug"],
            company=source["name"],
            raw_id=job.get("id"),
            title=job.get("title"),
            location=location,
            url=job.get("jobUrl"),
            apply_url=job.get("applyUrl") or job.get("jobUrl"),
            posted_at=job.get("publishedAt"),
            description=clean_text(job.get("descriptionPlain") or job.get("descriptionHtml")),
        ))
    return postings


def fetch_workable(source):
    data = http_get_json(
        f"https://apply.workable.com/api/v1/widget/accounts/{source['slug']}?details=true"
    )
    postings = []
    for job in data.get("jobs", []):
        parts = [job.get("city"), job.get("state"), job.get("country")]
        location = ", ".join(p for p in parts if p)
        if job.get("telecommuting"):
            location = f"{location} (Remote)".strip()
        postings.append(normalize(
            source=source["slug"],
            company=source["name"],
            raw_id=job.get("shortcode") or job.get("code") or job.get("title"),
            title=job.get("title"),
            location=location,
            url=job.get("url") or job.get("shortlink"),
            apply_url=job.get("application_url") or job.get("url"),
            posted_at=job.get("published_on"),
            description=clean_text(job.get("description")),
        ))
    return postings


def fetch_remoteok(source):
    data = http_get_json("https://remoteok.com/api")
    # The first element of the response is a legal notice, not a job.
    # RemoteOK asks that you link back to them when you show their listings.
    return [
        normalize(
            source="remoteok",
            company=job.get("company") or source["name"],
            raw_id=str(job.get("id")),
            title=job.get("position"),
            location=job.get("location") or "Remote",
            url=job.get("url"),
            apply_url=job.get("apply_url") or job.get("url"),
            posted_at=job.get("date"),
            description=clean_text(job.get("description")),
        )
        for job in data[1:]
        if job.get("id")
    ]


# ---------------------------------------------------------------------
# One common shape, so the rest of the pipeline never cares which platform
# a job came from.
# ---------------------------------------------------------------------
def normalize(source, company, raw_id, title, location, url, apply_url, posted_at, description=""):
    now = datetime.now(timezone.utc)
    # The ID is a hash of source + the platform's own job id. The title is left
    # out on purpose: if a recruiter edits the title, it is still the same job.
    job_id = hashlib.sha256(f"{source}:{raw_id}".encode()).hexdigest()[:16]
    return {
        "source": source,
        "job_id": job_id,
        "company": company or "",
        "title": title or "",
        "location": location or "",
        "url": url or "",
        "apply_url": apply_url or url or "",
        "posted_at": str(posted_at or ""),
        "description": description or "",
        "first_seen_at": now.isoformat(),
        "last_seen_at": now.isoformat(),
        "status": "new",  # the Matcher moves it to "scored", the Digest to "sent"
        "expires_at": expiry_timestamp(now),
    }


def expiry_timestamp(now):
    return int((now + timedelta(days=TTL_DAYS)).timestamp())


def save_posting(posting):
    """Returns "new" if the job was stored for the first time, "seen" if we
    already had it (in which case only its expiry is refreshed)."""
    table = get_table()
    try:
        table.put_item(Item=posting, ConditionExpression="attribute_not_exists(job_id)")
        return "new"
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    now = datetime.now(timezone.utc)
    table.update_item(
        Key={"source": posting["source"], "job_id": posting["job_id"]},
        UpdateExpression="SET expires_at = :exp, last_seen_at = :seen",
        ExpressionAttributeValues={":exp": expiry_timestamp(now), ":seen": now.isoformat()},
    )
    return "seen"
