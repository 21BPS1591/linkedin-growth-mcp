from linkedin_growth_mcp.strategy.hashtags import pick_hashtags

TEXT = "Manual expense approvals were the bottleneck. I automated the workflow with a data dashboard for the finance team."


def test_picks_relevant_capped_mix():
    tags = pick_hashtags(TEXT)
    assert 0 < len(tags) <= 4
    assert "#Automation" in tags
    assert "#IPO" not in tags


def test_proven_tag_outranks_weak_one():
    stats = {"#DataAnalytics": (5, 900.0), "#Automation": (5, 100.0), "#ProcessImprovement": (5, 500.0)}
    base = pick_hashtags(TEXT, limit=1)
    boosted = pick_hashtags(TEXT, limit=1, stats={"#FinanceTransformation": (5, 900.0), "#Automation": (5, 100.0)})
    assert base and boosted
    assert pick_hashtags(TEXT, stats=stats)


def test_unmeasured_tags_ignore_stats():
    assert pick_hashtags(TEXT, stats={"#Automation": (1, 1.0)}) == pick_hashtags(TEXT)
