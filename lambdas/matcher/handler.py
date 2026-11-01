"""
Matcher Lambda - Serverless Job Alert Pipeline

Finds postings the Fetcher saved (status "new"), scores each one, and marks
them "scored" so the Digest Builder can pick the best ones up.

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
    scored = skipped = 0

    for item in list(new_postings(table)):
        score, reason, flags = score_posting(item)
        try:
            table.update_item(
                Key={"source": item["source"], "job_id": item["job_id"]},
                UpdateExpression=(
                    "SET #s = :scored, score = :score, reason = :reason, "
                    "flags = :flags, scored_at = :now"
                ),
                ConditionExpression="#s = :new",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":scored": "scored", ":new": "new", ":score": score,
                    ":reason": reason, ":flags": flags,
                    ":now": datetime.now(timezone.utc).isoformat(),
                },
            )
            scored += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            skipped += 1  # another run got there first

    result = {"scored": scored, "skipped": skipped}
    print(json.dumps(result))
    return result
