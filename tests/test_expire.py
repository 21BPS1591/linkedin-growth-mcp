import time
from linkedin_growth_mcp.autopilot import Autopilot
from linkedin_growth_mcp.config import load_config
from linkedin_growth_mcp.store.db import Store


def test_stale_draft_deleted_and_idea_returns(tmp_path):
    st = Store(tmp_path / "t.db")
    iid = st.add_idea("story", "t", "b")
    did = st.add_draft("story", "number", "x" * 400, None)
    st.update_idea(iid, status="drafted", draft_id=did)
    st.conn.execute("UPDATE drafts SET created_at=? WHERE id=?", (time.time() - 7 * 3600, did))
    st.conn.commit()
    a = Autopilot.__new__(Autopilot)
    a.store = st
    a.posted_on = lambda d: True
    assert a.expire_stale_drafts() == 1
    assert st.get_draft(did) is None
    assert st.get_idea(iid)["status"] == "backlog"
