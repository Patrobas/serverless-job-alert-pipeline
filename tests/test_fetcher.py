"""
Tests for the Fetcher. Run from the repo root with:  python -m pytest tests -q

These check our own logic (filters, dedup, TTL refresh, normalizing).
The fetch tests use sample payloads shaped the way we believe each platform
responds. They prove our parsing is consistent, not that the real APIs still
look like this; the first live run is what confirms that.
"""

import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "fetcher"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["TABLE_NAME"] = "test-postings"

import filters  # noqa: E402
import handler  # noqa: E402


def posting(**overrides):
    base = handler.normalize(
        source="acme", company="Acme", raw_id="1", title="Implementation Consultant",
        location="Remote, Canada", url="https://x/1", apply_url="https://x/1/apply",
        posted_at="2026-10-30",
    )
    base.update(overrides)
    return base


# ---------------- filters ----------------
@pytest.mark.parametrize("title,expected", [
    ("Senior Implementation Consultant", True),
    ("Technical Program Manager, Platform", True),
    ("Solutions Engineer", True),
    ("Customer Success Manager", True),
    ("Product Manager, Public Sector", True),
    ("Software Engineer, Backend", False),
    ("Implementation Intern", False),
    ("Marketing Coordinator", False),
    ("Data Center Capacity Delivery, Canada", False),        # "delivery" alone is no longer a match
    ("Supply Chain Delivery Manager, Data Center Power", False),
])
def test_title_filter(title, expected):
    assert filters.title_ok(title) is expected


@pytest.mark.parametrize("location,expected", [
    ("Remote", True),
    ("Remote - US", True),                       # kept; the Matcher judges work authorization
    ("Massachusetts - Boston (Remote)", True),
    ("Remote-Friendly, United States", True),
    ("Remote-Friendly (Travel Required) | Canada", True),
    ("Canada - Toronto (Remote)", True),
    ("Toronto, ON", False),                      # other Canadian city, not remote: would mean relocating
    ("Chicago, Toronto", False),
    ("Ottawa", False),
    ("Toronto (Hybrid)", False),
    ("Toronto (Remote)", True),
    ("Calgary, Alberta, Canada", True),          # home city, any work mode
    ("Calgary, AB (Hybrid)", True),
    ("Canada", True),                            # no city named, work mode unknown
    ("Implementation Consultant (Remote or Onsite) Toronto", True),
    ("London, Ontario", False),                  # Canadian place, so never mistaken for London, UK
    ("London, Ontario (Remote)", True),
    ("Remote (US or UK)", True),                 # US mention overrides the UK
    ("Albuquerque, New Mexico (Remote)", True),  # "new mexico" is not "mexico city"
    ("Indianapolis, Indiana (Remote)", True),    # "indiana" is not "india"
    ("Milwaukee, WI (Remote)", True),            # "milwaukee" does not contain the word "uk"
    ("", True),                                  # unknown location is kept
    ("Hungary - Budapest (Remote)", False),
    ("Spain - Barcelona (Remote)", False),
    ("Denmark - Copenhagen (Remote)", False),
    ("United Kingdom - London (Remote)", False),
    ("France - Paris (Remote)", False),
    ("Dublin, Ireland", False),
    ("Remote - European Union", False),          # "european" is not "europe" as a whole word
    ("Remote (US or EU)", True),                 # US mention overrides
    ("Austin, TX", False),                       # not remote and not Canada
    ("Seattle, WA (On-site)", False),            # "on-site" must not be read as Ontario
])
def test_location_filter(location, expected):
    assert filters.location_ok(location) is expected


# ---------------- normalize ----------------
def test_job_id_is_stable_and_ignores_title_edits():
    a = handler.normalize("acme", "Acme", "42", "Old title", "", "", "", "")
    b = handler.normalize("acme", "Acme", "42", "New title", "", "", "", "")
    assert a["job_id"] == b["job_id"]


def test_job_id_differs_across_sources():
    a = handler.normalize("acme", "Acme", "42", "T", "", "", "", "")
    b = handler.normalize("other", "Other", "42", "T", "", "", "", "")
    assert a["job_id"] != b["job_id"]


# ---------------- DynamoDB behaviour ----------------
@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName="test-postings",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "source", "AttributeType": "S"},
                {"AttributeName": "job_id", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "source", "KeyType": "HASH"},
                {"AttributeName": "job_id", "KeyType": "RANGE"},
            ],
        )
        handler._table = None  # force a fresh handle inside the mock
        yield t
        handler._table = None


def test_first_save_is_new_second_is_seen(table):
    p = posting()
    assert handler.save_posting(p) == "new"
    assert handler.save_posting(p) == "seen"
    assert table.scan()["Count"] == 1


def test_seen_refreshes_expiry_without_touching_status(table):
    p = posting(expires_at=1)  # pretend it was about to expire
    handler.save_posting(p)
    table.update_item(
        Key={"source": p["source"], "job_id": p["job_id"]},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "scored"},
    )
    assert handler.save_posting(p) == "seen"
    item = table.get_item(Key={"source": p["source"], "job_id": p["job_id"]})["Item"]
    assert item["status"] == "scored"      # not reset to "new"
    assert int(item["expires_at"]) > 1     # expiry pushed forward


# ---------------- fetchers (sample payloads) ----------------
def test_greenhouse_parsing(monkeypatch):
    monkeypatch.setattr(handler, "http_get_json", lambda url: {"jobs": [
        {"id": 7, "title": "Solutions Architect", "location": {"name": "Remote, Canada"},
         "absolute_url": "https://boards.greenhouse.io/x/jobs/7", "updated_at": "2026-10-01"}]})
    jobs = handler.fetch_greenhouse({"name": "X", "slug": "x"})
    assert jobs[0]["title"] == "Solutions Architect"
    assert jobs[0]["location"] == "Remote, Canada"
    assert jobs[0]["apply_url"].endswith("/7")


def test_lever_parsing(monkeypatch):
    monkeypatch.setattr(handler, "http_get_json", lambda url: [
        {"id": "abc", "text": "Customer Success Manager", "categories": {"location": "Toronto"},
         "workplaceType": "remote", "hostedUrl": "https://jobs.lever.co/x/abc",
         "applyUrl": "https://jobs.lever.co/x/abc/apply", "createdAt": 1760000000000}])
    jobs = handler.fetch_lever({"name": "X", "slug": "x"})
    assert jobs[0]["location"] == "Toronto (Remote)"
    assert jobs[0]["apply_url"].endswith("/apply")


def test_ashby_parsing(monkeypatch):
    monkeypatch.setattr(handler, "http_get_json", lambda url: {"jobs": [
        {"id": "u1", "title": "Implementation Lead", "location": "Ottawa", "isRemote": True,
         "jobUrl": "https://jobs.ashbyhq.com/x/u1", "applyUrl": "https://jobs.ashbyhq.com/x/u1/application",
         "publishedAt": "2026-10-02"}]})
    jobs = handler.fetch_ashby({"name": "X", "slug": "x"})
    assert jobs[0]["location"] == "Ottawa (Remote)"


def test_workable_parsing(monkeypatch):
    monkeypatch.setattr(handler, "http_get_json", lambda url: {"jobs": [
        {"shortcode": "AB12", "title": "Technical Account Manager", "city": "Toronto",
         "state": "Ontario", "country": "Canada", "telecommuting": False,
         "url": "https://apply.workable.com/x/j/AB12/", "published_on": "2026-10-03"}]})
    jobs = handler.fetch_workable({"name": "X", "slug": "x"})
    assert jobs[0]["location"] == "Toronto, Ontario, Canada"


def test_remoteok_skips_legal_notice(monkeypatch):
    monkeypatch.setattr(handler, "http_get_json", lambda url: [
        {"legal": "notice"},
        {"id": 99, "position": "Solutions Engineer", "company": "Foo", "location": "",
         "url": "https://remoteok.com/99", "date": "2026-10-04"}])
    jobs = handler.fetch_remoteok({"name": "RemoteOK", "slug": "remoteok"})
    assert len(jobs) == 1 and jobs[0]["location"] == "Remote" and jobs[0]["company"] == "Foo"


# ---------------- handler end to end (mocked network) ----------------
def test_handler_counts_and_survives_one_broken_source(table, monkeypatch):
    def fake_fetch(source):
        if source["ats"] == "lever":
            raise RuntimeError("boom")
        return [posting(source=source["slug"], raw_id=source["slug"])]
    monkeypatch.setattr(handler, "fetch_source", fake_fetch)
    result = handler.lambda_handler({}, None)
    broken = sum(1 for s in handler.SOURCES if s["ats"] == "lever")
    assert len(result["errors"]) == broken
    assert result["new"] == len(handler.SOURCES) - broken


def test_handler_raises_when_every_source_fails(table, monkeypatch):
    def always_fail(source):
        raise RuntimeError("down")
    monkeypatch.setattr(handler, "fetch_source", always_fail)
    with pytest.raises(RuntimeError):
        handler.lambda_handler({}, None)
