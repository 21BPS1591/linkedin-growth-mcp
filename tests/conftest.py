import pytest

from linkedin_growth_mcp.autopilot import Autopilot


@pytest.fixture(autouse=True)
def _every_day_is_a_post_day(request, monkeypatch):
    """The autopilot only posts on weekdays. Older tests use the real clock, so they must not depend on the weekday they run on.
    test_hooks.py exercises the weekday and holiday rules themselves and keeps the real behaviour."""
    if request.module.__name__.endswith("test_hooks"):
        return
    monkeypatch.setattr(Autopilot, "is_post_day", lambda self, day=None: True)
