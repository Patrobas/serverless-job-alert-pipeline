import os
import sys

import boto3
from moto import mock_aws

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import clear_table  # noqa: E402


def test_clear_table_removes_everything():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="t", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "source", "AttributeType": "S"},
                                  {"AttributeName": "job_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "source", "KeyType": "HASH"},
                       {"AttributeName": "job_id", "KeyType": "RANGE"}],
        )
        for i in range(60):
            table.put_item(Item={"source": f"s{i % 3}", "job_id": str(i), "title": "x"})
        assert clear_table.clear_table(table) == 60
        assert table.scan()["Count"] == 0


def test_keep_sent_leaves_emailed_jobs_alone():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="t2", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "source", "AttributeType": "S"},
                                  {"AttributeName": "job_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "source", "KeyType": "HASH"},
                       {"AttributeName": "job_id", "KeyType": "RANGE"}],
        )
        for i in range(5):
            table.put_item(Item={"source": "s", "job_id": f"sent{i}", "status": "sent"})
        for i in range(7):
            table.put_item(Item={"source": "s", "job_id": f"open{i}", "status": "scored"})
        assert clear_table.clear_table(table, keep_sent=True) == 7
        remaining = table.scan()["Items"]
        assert len(remaining) == 5 and all(r["status"] == "sent" for r in remaining)
