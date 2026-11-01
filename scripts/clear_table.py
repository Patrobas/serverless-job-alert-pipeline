"""
Empty the postings table. For development only, e.g. after tuning filters or
scoring, so old test data doesn't end up in a real digest.

Usage:
  python3 scripts/clear_table.py                 delete everything
  python3 scripts/clear_table.py --keep-sent     delete everything EXCEPT jobs already emailed
                                                 (so they are not sent again)

Asks you to type the table name before deleting anything.
"""

import sys

import boto3


def clear_table(table, keep_sent=False):
    """Delete items (all, or all not yet emailed). Returns how many were deleted."""
    deleted = 0
    kwargs = {"ProjectionExpression": "#s, job_id, #st", "ExpressionAttributeNames": {"#s": "source", "#st": "status"}}
    with table.batch_writer(overwrite_by_pkeys=["source", "job_id"]) as batch:
        while True:
            page = table.scan(**kwargs)
            for item in page["Items"]:
                if keep_sent and item.get("status") == "sent":
                    continue
                batch.delete_item(Key={"source": item["source"], "job_id": item["job_id"]})
                deleted += 1
            if "LastEvaluatedKey" not in page:
                return deleted
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep_sent = "--keep-sent" in sys.argv
    name = args[0] if args else "job-alert-pipeline-postings"
    region = args[1] if len(args) > 1 else "us-east-1"
    if input(f"Type the table name ({name}) to delete {'unsent' if keep_sent else 'ALL'} items: ").strip() != name:
        sys.exit("Names did not match. Nothing deleted.")
    count = clear_table(boto3.resource("dynamodb", region_name=region).Table(name), keep_sent)
    print(f"Deleted {count} items from {name}.")
