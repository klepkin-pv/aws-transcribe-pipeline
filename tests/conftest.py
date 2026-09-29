"""Shared fixtures. The table schema mirrors infra/dynamodb.tf."""

import boto3
import pytest
from moto import mock_aws

from lib.storage import JobsRepository


@pytest.fixture
def repo():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="eu-central-1")
        table = dynamodb.create_table(
            TableName="jobs-test",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "gsi1-job-id",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="jobs-test")
        yield JobsRepository(table)
