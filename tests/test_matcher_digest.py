"""Tests for the Matcher, the scoring rules and the Digest Builder."""

import importlib.util
import json
import os
import sys

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
def test_ideal_job_scores_high_with_reason():
    score, reason, flags = scoring.score_posting(job())
    assert score >= 85 and flags == []
    assert "implementation" in reason.lower() and "remote in Canada" in reason


def test_us_remote_is_flagged_and_scores_lower():
    ca, _, _ = scoring.score_posting(job())
    us, reason, flags = scoring.score_posting(job(location="Remote - US"))
    assert us < ca
    assert "check_canada_eligibility" in flags and "confirm Canadians" in reason


def test_off_target_title_scores_low():
    score, _, _ = scoring.score_posting(job(title="Software Engineer", location="Remote"))
    assert score < 40


def test_penalties_stack_and_clamp_at_zero():
    score, _, _ = scoring.score_posting(job(title="VP, Account Executive", location="Austin, TX"))
    assert score == 0


def test_priority_company_bonus():
    plain, _, _ = scoring.score_posting(job(company="Acme"))
    boosted, reason, _ = scoring.score_posting(job(company="Ashby", title="Implementation Specialist"))
    assert boosted > plain - 1 and "priority company" in reason


def test_vp_does_not_match_inside_other_words():
    score, _, _ = scoring.score_posting(job(title="MVP Implementation Lead"))
    assert score >= 85  # "mvp" must not trigger the "vp" penalty


@pytest.mark.parametrize("location,expected_flag,min_score,max_score", [
    ("Canada - Toronto (Remote)", None, 85, 100),            # remote in Canada: best
    ("Toronto, ON", "not_remote", 40, 69),                    # Canadian city, not remote: below the bar
    ("Chicago, Toronto", "not_remote", 40, 69),               # office roles in a shared listing
    ("Toronto (Hybrid)", "not_remote", 40, 69),
    ("Calgary, Alberta (Hybrid)", None, 85, 100),             # home city hybrid: best
    ("Calgary, Alberta", "check_work_mode", 70, 84),          # home city, mode not listed: flagged
    ("Calgary, AB (On-site)", None, 55, 69),                  # home city but fully on-site: below the bar
    ("Canada", "check_work_mode", 80, 90),                    # Canada-wide, confirm remote
    ("Remote - North America", None, 80, 90),
    ("Remote - US", "check_canada_eligibility", 65, 75),
    ("US/Canada - Remote", None, 85, 100),                    # names Canada and remote
    ("Implementation Consultant (Remote or Onsite) Toronto", None, 85, 100),
])
def test_location_and_work_mode(location, expected_flag, min_score, max_score):
    score, _, flags = scoring.score_posting(job(location=location))
    assert min_score <= score <= max_score, score
    assert (expected_flag in flags) if expected_flag else flags == []


def test_engineer_titles_rank_below_consultant_titles():
    consultant, _, _ = scoring.score_posting(job(title="Forward Deployed Consultant"))
    engineer, reason, _ = scoring.score_posting(job(title="Forward Deployed Engineer"))
    assert engineer == consultant - 10 and "'engineer'" in reason


def test_engineering_in_a_title_does_not_trigger_engineer_penalty():
    plain, _, _ = scoring.score_posting(job(title="Implementation Lead"))
    other, _, _ = scoring.score_posting(job(title="Implementation Lead, Engineering Teams"))
    assert plain == other


def test_unknown_location_is_flagged_not_penalised_to_zero():
    score, _, flags = scoring.score_posting(job(location=""))
    assert "location_unknown" in flags and score > 50


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
    assert matcher.lambda_handler({}, None) == {"scored": 1, "skipped": 0}
    item = table.get_item(Key={"source": "acme", "job_id": "a"})["Item"]
    assert item["status"] == "scored" and int(item["score"]) >= 85 and item["reason"]
    assert status_of(table, "b") == "sent"          # untouched


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


def test_digest_sends_nothing_when_nothing_qualifies(table, monkeypatch):
    sent = []
    monkeypatch.setattr(digest_handler, "publish", lambda s, b: sent.append(1))
    table.put_item(Item=job(status="scored", score=10, reason="Weak"))
    assert digest_handler.lambda_handler({}, None) == {"sent": 0}
    assert sent == []


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
    table.put_item(Item=job(job_id="x", company="Other", title="Other role", status="scored", score=60, reason="r"))
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


@pytest.mark.parametrize("title", ["Sales Development Representative", "Marketing Implementation Lead", "Campaign Onboarding Manager"])
def test_sales_and_marketing_titles_are_penalised(title):
    plain, _, _ = scoring.score_posting(job(title="Implementation Consultant"))
    score, _, _ = scoring.score_posting(job(title=title))
    assert score < plain


def test_us_only_remote_scores_below_the_bar():
    score, _, _ = scoring.score_posting(job(title="Implementation Consultant", location="Boston, MA (Remote)"))
    assert score < 70
