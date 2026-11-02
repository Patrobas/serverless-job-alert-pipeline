"""Tests for the Matcher, the scoring rules and the Digest Builder."""

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

ROOT = os.path.join(os.path.dirname(__file__), "..", "lambdas")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def load(name, folder, filename):
    """Load a module by path under a unique name (three folders each have a handler.py)."""
    folder_path = os.path.abspath(os.path.join(ROOT, folder))
    sys.path.insert(0, folder_path)
    try:
        spec = importlib.util.spec_from_file_location(name, os.path.join(folder_path, filename))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(folder_path)


scoring = load("matcher_scoring", "matcher", "scoring.py")
matcher = load("matcher_handler", "matcher", "handler.py")
digest = load("digest_format", "digest_builder", "digest.py")
sys.modules["digest"] = digest  # the digest handler imports it by this name
digest_handler = load("digest_handler", "digest_builder", "handler.py")


def job(**kw):
    base = {"source": "acme", "job_id": "1", "company": "Acme", "title": "Implementation Consultant",
            "location": "Canada - Toronto (Remote)", "url": "https://x/1", "apply_url": "https://x/1/apply",
            "status": "new"}
    base.update(kw)
    return base


# ---------------- scoring ----------------
def padded(text):
    """Descriptions shorter than 500 characters are flagged low confidence."""
    return text + " " + "Filler sentence about the team and the company. " * 15


def score(**kw):
    return scoring.score_posting(job(**kw))


def test_ideal_job_scores_high_with_matched_signals():
    r = score(description=padded("You have 3 years of experience with API and FHIR integration work in healthcare SaaS. "
                                 "Salary $150,000."), posted_at="2026-10-30T10:00:00Z")
    assert not r["dropped"] and r["score"] >= 85
    assert "implementation consultant" in r["reason"] and "remote in Canada" in r["reason"]
    assert r["years_required"] == 3 and r["salary_max"] == 150000 and r["work_type"] == "Remote"
    assert r["red_flags"] == [] and not r["low_confidence"]
    assert sum(item["points"] for item in r["breakdown"]) >= r["score"]


@pytest.mark.parametrize("title", ["Sales Development Representative", "Director of Implementation",
                                   "Privacy and AI Program Manager", "Staff Software Engineer", "Legal Counsel"])
def test_excluded_titles_are_dropped(title):
    r = score(title=title, description=padded("Remote."))
    assert r["dropped"] and "title" in r["reason"]


def test_vp_does_not_match_inside_other_words():
    assert not score(title="MVP Implementation Consultant", description=padded("x"))["dropped"]


def test_eastern_time_zone_is_a_hard_fail():
    r = score(description=padded("You must be based in the Eastern time zone."))
    assert r["dropped"] and "eastern" in r["reason"].lower()


@pytest.mark.parametrize("phrase", ["Mountain time", "any Canadian time zone", "a flexible time zone"])
def test_time_zone_override_phrases_keep_the_job(phrase):
    r = score(description=padded(f"Must be based in the Eastern time zone, or work in {phrase}."))
    assert not r["dropped"]


def test_hybrid_without_calgary_is_a_hard_fail():
    r = score(location="Canada (Remote)",
              description=padded("This is a hybrid role in our Edmonton, Toronto, Vancouver or Kitchener office."))
    assert r["dropped"] and "hybrid" in r["reason"]


def test_hybrid_with_calgary_listed_survives():
    r = score(location="Calgary, AB", description=padded("Hybrid role, three days in office in Calgary."))
    assert not r["dropped"]


def test_hybrid_override_when_company_is_remote_first():
    r = score(description=padded("We are remote-first. Some teams meet in office quarterly."))
    assert not r["dropped"]


@pytest.mark.parametrize("text,years", [("5+ years of experience", 5), ("minimum 8 years of PM experience", 8),
                                        ("3-7 years experience required", 7), ("Founded 12 years ago", None)])
def test_years_extraction(text, years):
    assert scoring.extract_years(text) == years


def test_years_change_the_score():
    low = score(description=padded("2 years of experience required."))["score"]
    mid = score(description=padded("6 years of experience required."))["score"]
    high = score(description=padded("9 years of experience required."))["score"]
    assert low == mid + 10 and mid == high + 25


def test_weak_title_only_counts_with_five_years_or_fewer():
    base = dict(title="Senior Product Manager", location="Canada (Remote)")
    ok = score(description=padded("4 years of experience required."), **base)
    no = score(description=padded("8 years of experience required."), **base)
    assert ok["score"] > 0 and any("weak" in i["rule"] for i in ok["breakdown"])
    assert not any("weak" in i["rule"] for i in no["breakdown"])


def test_skill_bonus_is_capped():
    many = "api fhir hl7 integration healthcare ehr saas aws cloud onboarding go live workflow configuration"
    r = score(description=padded(many))
    skill_pts = [i["points"] for i in r["breakdown"] if i["rule"].startswith("skills")]
    assert skill_pts == [20]


def test_each_red_flag_costs_twenty():
    clean = score(description=padded("API work."))["score"]
    flagged = score(description=padded("API work. Strong Python and LangChain required."))
    assert flagged["red_flags"] == ["strong python", "langchain"]
    assert flagged["score"] == clean - 40


def test_salary_and_recency_nudges():
    base = score(description=padded("API work."))["score"]
    assert score(description=padded("API work. Pay is $95,000."))["score"] == base - 10
    assert score(description=padded("API work. Pay up to $160k."))["score"] == base + 5
    recent = scoring.score_posting(job(description=padded("API work."), posted_at="2026-10-30T00:00:00Z"),
                                   now=scoring.datetime(2026, 11, 1, tzinfo=scoring.timezone.utc))
    assert recent["score"] == base + 5


def test_missing_or_short_description_is_low_confidence_not_penalised():
    full = score(description=padded("x"))
    short = score(description="")
    assert short["low_confidence"] and "low_confidence" in short["flags"]
    assert not full["low_confidence"]
    assert short["score"] == full["score"]


@pytest.mark.parametrize("location,expected_flag,low,high", [
    ("Canada (Remote)", None, 55, 100),
    ("Calgary, AB (Hybrid)", None, 55, 100),
    ("Calgary, AB", "check_work_mode", 45, 100),
    ("Boston, MA (Remote)", "check_canada_eligibility", 0, 55),
    ("USA / Canada (Remote)", None, 55, 100),
])
def test_location_and_work_mode(location, expected_flag, low, high):
    r = score(location=location, description=padded("x"))
    assert not r["dropped"] and low <= r["score"] <= high
    if expected_flag:
        assert expected_flag in r["flags"]


def test_canadian_city_that_is_not_remote_is_dropped():
    assert score(location="Toronto, ON", description=padded("x"))["dropped"]


def test_unknown_location_is_flagged_not_dropped():
    r = score(location="", description=padded("x"))
    assert not r["dropped"] and "location_unknown" in r["flags"]


# ---- the six postings from the Nov 1 digest ----
def nov1(title, company, location, body):
    return scoring.score_posting(job(title=title, company=company, location=location, description=padded(body)))


def test_nov1_veeva_ai_roles_fall_below_the_bar():
    a = nov1("Senior Technical Product Manager, AI", "Veeva", "Canada (Remote)",
             "2+ years of experience with LLM work. Strong Python coding skills and LangChain required.")
    b = nov1("Senior Product Manager, PromoMats", "Veeva", "Canada (Remote)",
             "AI coding skills required. Marketing background. Experience with LangChain.")
    assert not a["dropped"] and a["score"] < 65 and "langchain" in a["red_flags"]
    assert b["score"] < 65


def test_nov1_veeva_medical_inquiry_is_dropped():
    r = nov1("Senior Product Manager, Medical Inquiry", "Veeva", "Canada (Remote)",
             "8+ years of PM experience. Must be based in the Eastern time zone.")
    assert r["dropped"]


def test_nov1_jobber_supply_is_dropped():
    r = nov1("Senior Product Manager, Supply", "Jobber", "Canada (Remote)",
             "Hybrid role for Edmonton, Toronto, Vancouver or Kitchener Waterloo. 5 years of experience.")
    assert r["dropped"]


def test_nov1_visier_privacy_is_dropped():
    r = nov1("Privacy and AI Program Manager", "Visier", "Canada (Remote)",
             "4+ years of experience running GDPR and CCPA programs.")
    assert r["dropped"]


def test_nov1_procurify_is_the_only_one_that_can_make_it():
    """Under the v2 weights a weak title (product manager) tops out at 65, so Procurify only
    reaches the digest with every bonus. Either way no other Nov 1 posting can."""
    r = nov1("Senior Product Manager", "Procurify", "Canada (Remote)",
             "5 years of experience in SaaS product work with API and integration projects.")
    others = [
        nov1("Senior Technical Product Manager, AI", "Veeva", "Canada (Remote)",
             "2+ years of experience with LLM work. Strong Python coding skills and LangChain required."),
        nov1("Senior Product Manager, PromoMats", "Veeva", "Canada (Remote)",
             "AI coding skills required. Marketing background. Experience with LangChain."),
    ]
    assert not r["dropped"] and "remote in Canada" in r["reason"]
    assert all(o["score"] < r["score"] for o in others)
    best = scoring.score_posting(
        job(title="Senior Product Manager", location="Canada (Remote)", posted_at="2026-10-30T00:00:00Z",
            description=padded("5 years of experience. api fhir hl7 integration healthcare ehr saas. Salary $160,000.")),
        now=scoring.datetime(2026, 11, 1, tzinfo=scoring.timezone.utc))
    assert best["score"] == 65  # the ceiling for a weak title: every bonus at once


def test_canada_keywords_match_fetcher_profile():
    """The two configs hold their own copy of the Canada list. This stops them drifting apart."""
    fetcher_profile = json.load(open(os.path.join(ROOT, "fetcher", "profile.json")))
    assert set(fetcher_profile["canada_keywords"]) == set(scoring.CONFIG["canada_keywords"])
    assert fetcher_profile["province_codes"] == scoring.CONFIG["province_codes"]
    assert fetcher_profile["home_cities"] == scoring.CONFIG["home_cities"]


# ---------------- DynamoDB-backed handlers ----------------
@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "t")
    with mock_aws():
        t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="t", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "source", "AttributeType": "S"},
                                  {"AttributeName": "job_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "source", "KeyType": "HASH"},
                       {"AttributeName": "job_id", "KeyType": "RANGE"}],
        )
        matcher._table = None
        digest_handler._table = None
        yield t
        matcher._table = None
        digest_handler._table = None


def status_of(table, job_id):
    return table.get_item(Key={"source": "acme", "job_id": job_id})["Item"]["status"]


def test_matcher_scores_only_new_jobs(table):
    table.put_item(Item=job(job_id="a"))
    table.put_item(Item=job(job_id="b", status="sent"))
    assert matcher.lambda_handler({}, None) == {"scored": 1, "dropped": 0, "skipped": 0}
    item = table.get_item(Key={"source": "acme", "job_id": "a"})["Item"]
    assert item["status"] == "scored" and int(item["score"]) >= 60 and item["reason"]
    assert item["breakdown"] and item["work_type"] == "Remote" and item["red_flags"] == []
    assert status_of(table, "b") == "sent"          # untouched


def test_matcher_drops_hard_failed_jobs_and_stores_breakdown(table):
    table.put_item(Item=job(job_id="d", title="Director of Implementation"))
    assert matcher.lambda_handler({}, None) == {"scored": 0, "dropped": 1, "skipped": 0}
    item = table.get_item(Key={"source": "acme", "job_id": "d"})["Item"]
    assert item["status"] == "dropped" and item["reason"].startswith("Dropped:")


def test_matcher_second_run_does_nothing(table):
    table.put_item(Item=job())
    matcher.lambda_handler({}, None)
    assert matcher.lambda_handler({}, None)["scored"] == 0


def test_digest_sends_one_email_with_best_first_and_marks_sent(table, monkeypatch):
    sent = []
    monkeypatch.setattr(digest_handler, "publish", lambda subject, body: sent.append((subject, body)))
    monkeypatch.setenv("MIN_SCORE", "50")
    table.put_item(Item=job(job_id="hi", status="scored", score=90, reason="Great fit"))
    table.put_item(Item=job(job_id="mid", status="scored", score=60, reason="OK fit", title="Product Manager"))
    table.put_item(Item=job(job_id="low", status="scored", score=30, reason="Weak"))
    table.put_item(Item=job(job_id="old", status="sent", score=95, reason="Already sent"))

    assert digest_handler.lambda_handler({}, None)["sent"] == 2
    assert len(sent) == 1
    body = sent[0][1]
    assert body.index("Great fit") < body.index("OK fit")
    assert "Read the job: https://x/1" in body and "Apply: https://x/1/apply" in body
    assert "Weak" not in body and "Already sent" not in body
    assert status_of(table, "hi") == "sent" and status_of(table, "mid") == "sent"
    assert status_of(table, "low") == "scored"      # below the bar, stays put


def test_digest_sends_status_email_when_nothing_qualifies(table, monkeypatch):
    sent = []
    monkeypatch.setattr(digest_handler, "publish", lambda s, b: sent.append((s, b)))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    table.put_item(Item=job(job_id="w", status="scored", score=10, reason="Weak", first_seen_at=today + "T14:00:00+00:00"))
    table.put_item(Item=job(job_id="s", status="sent", score=90, first_seen_at="2020-01-01T00:00:00+00:00"))
    table.put_item(Item=job(job_id="d", status="dropped", first_seen_at="2020-01-01T00:00:00+00:00"))
    assert digest_handler.lambda_handler({}, None) == {"sent": 0, "status_email": 1}
    assert len(sent) == 1
    subject, body = sent[0]
    assert subject.endswith("no new matches") and "Personalized AWS Job Digest" in subject
    assert "New postings found today: 1" in body and "Postings tracked in total: 3" in body
    assert "Already emailed to you: 1" in body and "Dropped by the hard rules: 1" in body
    assert "(highest 10)" in body
    assert status_of(table, "w") == "scored"        # nothing is changed by a status email


def test_status_email_failure_raises_so_the_alarm_fires(table, monkeypatch):
    def boom(s, b):
        raise RuntimeError("sns down")
    monkeypatch.setattr(digest_handler, "publish", boom)
    with pytest.raises(RuntimeError):
        digest_handler.lambda_handler({}, None)


def test_status_email_with_empty_table(table, monkeypatch):
    sent = []
    monkeypatch.setattr(digest_handler, "publish", lambda s, b: sent.append(b))
    digest_handler.lambda_handler({}, None)
    assert "Postings tracked in total: 0" in sent[0] and "(highest" not in sent[0]


def test_digest_respects_max_items(table, monkeypatch):
    sent = []
    monkeypatch.setattr(digest_handler, "publish", lambda s, b: sent.append(b))
    monkeypatch.setenv("MAX_ITEMS", "2")
    for i in range(5):
        table.put_item(Item=job(job_id=str(i), company=f"Co{i}", title=f"Role {i}",
                                status="scored", score=80 + i, reason="r"))
    result = digest_handler.lambda_handler({}, None)
    assert result == {"sent": 2, "merged_duplicates": 0, "left_for_next_time": 3}
    assert status_of(table, "4") == "sent" and status_of(table, "0") == "scored"


def test_digest_merges_same_role_in_several_cities(table, monkeypatch):
    sent = []
    monkeypatch.setattr(digest_handler, "publish", lambda s, b: sent.append(b))
    for i, city in enumerate(["Boston", "Chicago", "Atlanta"]):
        table.put_item(Item=job(job_id=str(i), title="Senior Consultant", location=f"{city}, US (Remote)",
                                status="scored", score=80 - i, reason="r"))
    result = digest_handler.lambda_handler({}, None)
    assert result == {"sent": 1, "merged_duplicates": 2, "left_for_next_time": 0}
    assert "Same role also listed in: Chicago, US (Remote); Atlanta, US (Remote)" in sent[0]
    assert all(status_of(table, str(i)) == "sent" for i in range(3))


def test_digest_caps_roles_per_company(table, monkeypatch):
    sent = []
    monkeypatch.setattr(digest_handler, "publish", lambda s, b: sent.append(b))
    monkeypatch.setenv("MAX_PER_COMPANY", "2")
    for i in range(4):
        table.put_item(Item=job(job_id=str(i), title=f"Role {i}", status="scored", score=90 - i, reason="r"))
    table.put_item(Item=job(job_id="x", company="Other", title="Other role", status="scored", score=70, reason="r"))
    result = digest_handler.lambda_handler({}, None)
    assert result == {"sent": 3, "merged_duplicates": 0, "left_for_next_time": 2}
    assert status_of(table, "2") == "scored" and status_of(table, "x") == "sent"


def test_digest_keeps_jobs_unsent_if_publish_fails(table, monkeypatch):
    def boom(subject, body):
        raise RuntimeError("SNS down")
    monkeypatch.setattr(digest_handler, "publish", boom)
    table.put_item(Item=job(status="scored", score=90, reason="r"))
    with pytest.raises(RuntimeError):
        digest_handler.lambda_handler({}, None)
    assert status_of(table, "1") == "scored"


def test_remoteok_attribution_appears_when_needed():
    _, body = digest.build_digest([{"score": 70, "title": "T", "company": "C", "source": "remoteok",
                                    "apply_url": "https://remoteok.com/1", "reason": "r"}], 1)
    assert "remoteok.com" in body


def test_digest_does_not_repeat_link_when_read_and_apply_are_the_same():
    _, body = digest.build_digest([{"score": 80, "title": "T", "company": "C", "source": "x",
                                    "url": "https://x/j", "apply_url": "https://x/j", "reason": "r"}], 1)
    assert body.count("https://x/j") == 1 and "Read the job:" in body and "Apply:" not in body


def test_digest_why_line_shows_signals_red_flags_and_details():
    _, body = digest.build_digest([{"score": 72, "title": "T", "company": "C", "source": "x", "url": "https://x/j",
                                    "reason": "Title matches 'x'; remote in Canada.", "red_flags": ["langchain"],
                                    "years_required": 5, "work_type": "Remote", "salary_max": 140000,
                                    "low_confidence": True}], 1)
    assert "Why: Title matches 'x'; remote in Canada." in body
    assert "Red flags: langchain" in body
    assert "Work type: Remote | Years required: 5 | Salary up to: $140,000" in body
    assert "Low confidence" in body


def test_digest_handles_missing_details():
    _, body = digest.build_digest([{"score": 72, "title": "T", "company": "C", "source": "x", "url": "https://x/j",
                                    "reason": "r"}], 1)
    assert "Red flags: none found" in body and "Years required: not listed" in body
    assert "Salary: not listed" in body and "Low confidence" not in body
