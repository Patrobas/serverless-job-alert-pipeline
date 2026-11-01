"""
Matcher Lambda - Serverless Job Alert Pipeline

Finds postings the Fetcher saved (status "new"), scores each one, and marks
them "scored" (or "dropped" when a hard rule rules them out) so the Digest Builder can pick the best ones up.

The status change uses a condition ("only if still new"), so if two runs
overlap, a job is never scored twice.
"""

import json
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from scoring import score_posting

_table = None


def get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def new_postings(table):
    """Every posting still waiting to be scored. A Scan reads the whole table
    and filters afterwards; fine at hundreds of rows, a secondary index on
    status is the upgrade if the table grows."""
    kwargs = {"FilterExpression": Attr("status").eq("new")}
    while True:
        page = table.scan(**kwargs)
        yield from page["Items"]
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def lambda_handler(event, context):
    table = get_table()
    scored = dropped = skipped = 0

    for item in list(new_postings(table)):
        result = score_posting(item)
        status = "dropped" if result["dropped"] else "scored"
        try:
            table.update_item(
                Key={"source": item["source"], "job_id": item["job_id"]},
                UpdateExpression=(
                    "SET #s = :status, score = :score, reason = :reason, "
                    "flags = :flags, red_flags = :red_flags, breakdown = :breakdown, "
                    "years_required = :years, work_type = :work_type, "
                    "salary_max = :salary, low_confidence = :low, scored_at = :now"
                ),
                ConditionExpression="#s = :new",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": status, ":new": "new", ":score": result["score"],
                    ":reason": result["reason"], ":flags": result["flags"],
                    ":red_flags": result["red_flags"], ":breakdown": result["breakdown"],
                    ":years": result["years_required"], ":work_type": result["work_type"],
                    ":salary": result["salary_max"], ":low": result["low_confidence"],
                    ":now": datetime.now(timezone.utc).isoformat(),
                },
            )
            if result["dropped"]:
                dropped += 1
            else:
                scored += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            skipped += 1  # another run got there first

    out = {"scored": scored, "dropped": dropped, "skipped": skipped}
    print(json.dumps(out))
    return out
