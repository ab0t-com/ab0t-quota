"""H2: QuotaStore endpoint_url validation tests."""

import pytest

from ab0t_quota.persistence import QuotaStore


class TestEndpointUrlValidation:
    """H2: endpoint_url must be restricted to known dev hosts (SSRF protection)."""

    def test_none_accepted(self):
        """Production: endpoint_url=None uses default AWS endpoint."""
        store = QuotaStore(endpoint_url=None)
        assert store._endpoint_url is None

    def test_localhost_accepted(self):
        store = QuotaStore(endpoint_url="http://localhost:8000")
        assert store._endpoint_url == "http://localhost:8000"

    def test_127_0_0_1_accepted(self):
        store = QuotaStore(endpoint_url="http://127.0.0.1:8000")
        assert store._endpoint_url == "http://127.0.0.1:8000"

    def test_dynamodb_local_accepted(self):
        store = QuotaStore(endpoint_url="http://dynamodb-local:8000")
        assert store._endpoint_url == "http://dynamodb-local:8000"

    def test_dynamodb_docker_accepted(self):
        store = QuotaStore(endpoint_url="http://dynamodb:8000")
        assert store._endpoint_url == "http://dynamodb:8000"

    def test_localstack_accepted(self):
        store = QuotaStore(endpoint_url="http://localstack:4566")
        assert store._endpoint_url == "http://localstack:4566"

    def test_arbitrary_host_rejected(self):
        with pytest.raises(ValueError, match="not in allowlist"):
            QuotaStore(endpoint_url="http://attacker.com:8000")

    def test_internal_ip_rejected(self):
        with pytest.raises(ValueError, match="not in allowlist"):
            QuotaStore(endpoint_url="http://10.0.0.5:8000")

    def test_production_aws_endpoint_rejected(self):
        """Direct AWS endpoints should use endpoint_url=None instead."""
        with pytest.raises(ValueError, match="not in allowlist"):
            QuotaStore(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")
