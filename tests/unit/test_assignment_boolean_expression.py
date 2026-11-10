import pytest

from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.structural import AssignmentDetector


@pytest.mark.parametrize("operator", ["or", "and"])
def test_unquoted_identifier_boolean_expression_is_code(operator):
    text = f'context_length(model, access_token=demoarg {operator} "", base_url=url)'
    assert not list(AssignmentDetector().detect(text, DetectContext()))


@pytest.mark.parametrize(
    "text",
    [
        'access_token="demoarg"',
        "ACCESS_TOKEN=demoarg",
        "log access_token=demoarg",
        "log access_token=demoarg or retry",
        "log(foo) access_token=demoarg or retry",
        '"context_length(model, access_token=demoarg or retry)"',
        "'context_length(model, access_token=demoarg and retry)'",
        "log(foo); access_token=demoarg or retry",
        "log(foo)\naccess_token=demoarg or retry",
        'context_length(model, access_token="demoarg" or "")',
        'context_length(model, ACCESS_TOKEN=demoarg or "")',
        'context_length(model, access_token=Actual-Secret-123 or "")',
    ],
)
def test_literal_or_env_secret_still_detected(text):
    assert list(AssignmentDetector().detect(text, DetectContext()))
