"""Who decides on what lowers protection, as Latet Enterprise's settings say it (E5): each person, or the security team;
nothing else is taken, and the generic settings route sends to E5's own."""

import pytest
from pydantic import ValidationError

from latet.ee.settings import ApprovalsSettings, EnterpriseSettings

from ..people.server_support import ADMIN_EE, as_admin
from ..servers import services


def test_the_settings_say_who_decides_and_nothing_else():
    assert EnterpriseSettings.model_validate({}).approvals == ApprovalsSettings()
    settings = EnterpriseSettings.model_validate({"approvals": {"changes": "security"}})
    assert (settings.approvals.changes, settings.approvals.live_review) == ("security", "self")
    for wrong in ({"changes": "boss"}, {"changes": "security", "timeout": 60}, {"live_review": "controllers"}):
        with pytest.raises(ValidationError):
            EnterpriseSettings.model_validate({"approvals": wrong})


@pytest.mark.server
@services
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_the_generic_settings_route_sends_to_latet_server_approvals(cluster, method):
    answer = cluster.a.request(
        method, f"{ADMIN_EE}/settings/approvals", json={"changes": "self"}, headers=as_admin(cluster)
    )
    assert answer.status_code == 409 and "latet server approvals set" in answer.json()["error"]
