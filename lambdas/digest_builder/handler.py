"""
Digest Builder Lambda - Serverless Job Alert Pipeline

Takes jobs the Matcher scored, keeps the best ones, sends ONE email through
SNS, and marks them "sent" so they are never emailed again.

Jobs below MIN_SCORE stay "scored" and are never sent; they are still in the
table, so the scoring rules can be tuned and they can be revisited.
"""

import json
import os

import boto3
from boto3.dynamodb.conditions import Attr

from digest import build_digest

_table = None
_sns = None


def get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def publish(subject, message):
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    _sns.publish(TopicArn=os.environ["TOPIC_ARN"], Subject=subject, Message=message)


def scored_postings(table, min_score):
    kwargs = {"FilterExpression": Attr("status").eq("scored") & Attr("score").gte(min_score)}
    while True:
        page = table.scan(**kwargs)
        yield from page["Items"]
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def pick_jobs(candidates, max_items, max_per_company):
    """candidates is sorted best first. Returns (jobs to show, every job to mark sent).

    - The same role listed in several places counts once, and the other places
      are shown on that entry ("also listed in").
    - No more than max_per_company different roles from one company, so one
      employer cannot fill the whole email. The rest wait for the next run.
    """
    chosen, per_company, keys = [], {}, {}
    for job in candidates:
        key = (job["company"].lower(), job["title"].lower())
        company = key[0]
        if key in keys or len(chosen) >= max_items or per_company.get(company, 0) >= max_per_company:
            continue
        keys[key] = {**job, "also_in": []}
        chosen.append(keys[key])
        per_company[company] = per_company.get(company, 0) + 1

    to_mark = []
    for job in candidates:
        key = (job["company"].lower(), job["title"].lower())
        if key in keys:
            to_mark.append(job)
            entry = keys[key]
            if job["job_id"] != entry["job_id"] and job.get("location") \
                    and job["location"] != entry.get("location"):
                entry["also_in"].append(job["location"])
    return chosen, to_mark


def lambda_handler(event, context):
    min_score = int(os.environ.get("MIN_SCORE", "65"))
    max_items = int(os.environ.get("MAX_ITEMS", "8"))
    max_per_company = int(os.environ.get("MAX_PER_COMPANY", "3"))
    table = get_table()

    candidates = sorted(scored_postings(table, min_score), key=lambda j: -int(j["score"]))
    if not candidates:
        print(json.dumps({"sent": 0, "note": "nothing to send"}))
        return {"sent": 0}

    chosen, to_mark = pick_jobs(candidates, max_items, max_per_company)
    subject, body = build_digest(chosen, total_matches=len(candidates))

    # Publish first, mark sent second. If marking fails, the worst case is
    # a repeat email next run, never a silently lost one.
    publish(subject, body)

    for job in to_mark:
        table.update_item(
            Key={"source": job["source"], "job_id": job["job_id"]},
            UpdateExpression="SET #s = :sent",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":sent": "sent"},
        )

    result = {"sent": len(chosen), "merged_duplicates": len(to_mark) - len(chosen),
              "left_for_next_time": len(candidates) - len(to_mark)}
    print(json.dumps(result))
    return result
