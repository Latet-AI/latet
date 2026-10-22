"""SigV4: Latet signs Bedrock requests as AWS's own library does (botocore's signatures, recorded)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from latet.gateway.sigv4 import AwsCredentials, canonical_query, canonical_uri, sign

VECTORS = json.loads((Path(__file__).parents[1] / "data" / "sigv4" / "bedrock.json").read_text("utf-8"))["vectors"]


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_latet_signs_as_aws_does(vector):
    credentials = AwsCredentials(vector["access_key"], vector["secret_key"], vector["token"])
    now = datetime.strptime(vector["amz_date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    method, url, body = vector["method"], vector["url"], vector["body"].encode()
    headers = dict(sign(method, url, body, credentials, vector["region"], now=now))
    assert headers["authorization"] == vector["authorization"]
    assert headers["x-amz-date"] == vector["amz_date"]
    assert headers.get("x-amz-security-token") == vector["x-amz-security-token"]


def test_the_path_is_signed_encoded_once_more():
    assert canonical_uri("/model/a%3Ab/invoke") == "/model/a%253Ab/invoke"
    assert canonical_uri("/model/a:b/invoke") == "/model/a%3Ab/invoke"
    assert canonical_uri("/model/./a//b/../c") == "/model/a/c"
    assert canonical_uri("") == "/"


def test_the_query_is_signed_sorted_and_encoded():
    query = "type=SYSTEM_DEFINED&nextToken=ab%2Bcd%2Fef%3D&maxResults=10"
    assert canonical_query(query) == "maxResults=10&nextToken=ab%2Bcd%2Fef%3D&type=SYSTEM_DEFINED"
    assert canonical_query("b=2&a") == "a=&b=2"
    assert canonical_query("") == ""


def test_credentials_never_show_their_secrets():
    credentials = AwsCredentials("AKIDEXAMPLE", "the-secret-key", "the-session-token")
    assert "secret" not in repr(credentials) and "session" not in repr(credentials)
