"""The picker, not the drafting model, decides the hashtags on a non-IPO post."""
from linkedin_growth_mcp.linkedin.publish import publish_post
from linkedin_growth_mcp.strategy.hashtags import pick_hashtags

BODY = "I automated the manual expense approval workflow for the finance team and built a data dashboard."


def test_model_tags_are_replaced_by_the_chosen_set(monkeypatch):
    sent = {}

    class R:
        headers = {"x-restli-id": "urn:li:share:1"}

        def raise_for_status(self):
            pass

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["commentary"] = json["commentary"]
        return R()

    monkeypatch.setattr("linkedin_growth_mcp.linkedin.publish.load_token",
                        lambda cfg: {"person_id": "p", "access_token": "t"})
    monkeypatch.setattr("linkedin_growth_mcp.linkedin.publish.httpx.post", fake_post)

    class Cfg:
        linkedin_api_version = "202610"
        brand_hashtag = "#IPOByTheNumbers"
        reach_hashtags = "#IPO"

    publish_post(Cfg(), BODY + "\n\n#Investing #Crypto", None, ipo_series=False, tags=["#Automation", "#DataAnalytics"])
    out = sent["commentary"]
    assert "#Automation" in out and "#DataAnalytics" in out
    assert "#Investing" not in out and "#Crypto" not in out
    assert "#IPO" not in out


def test_picker_finds_tags_for_a_work_story():
    assert pick_hashtags(BODY)
