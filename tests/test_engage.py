from linkedin_growth_mcp.linkedin import engage

POST = ("Feed post\n\nAsha Rao\n\n \u2022 2nd\n\nSenior Analyst at Acme | Power BI\n\n5h \u2022 \n\nFollow\n\n"
        + "I rebuilt our month end report in Power BI and cut the refresh from 40 minutes to 4. " * 3 + "\n\u2026 more\n\n120\n14\n3")


def card(**kw):
    c = engage.parse_card(POST, 120, 14, 3)
    c.update(kw)
    return c


def pick(cards, **kw):
    args = dict(owner="Me Myself", done_keys=set(), recent_authors=set(), banned=[])
    args.update(kw)
    return engage.pick_candidates(cards, **args)


def test_parse_card_reads_author_age_body_and_counts():
    c = engage.parse_card(POST, 120, 14, 3)
    assert c["author"] == "Asha Rao" and c["is_person"] and c["age_h"] == 5
    assert c["text"].startswith("I rebuilt our month end report") and "\u2026 more" not in c["text"] and not c["text"].endswith("3")
    assert (c["reactions"], c["comments"], c["reposts"]) == (120, 14, 3)


def test_company_pages_and_junk_are_not_cards():
    assert engage.parse_card("Feed post\n\nZoomCharts\n\nsome text", 5, 0, 0) is None
    co = engage.parse_card(POST.replace(" \u2022 2nd", ""), 120, 14, 3)
    assert co is not None and not co["is_person"] and pick([co]) == []


def test_pick_filters():
    good = card()
    assert pick([good]) == [good]
    assert pick([good], owner="asha rao") == []                         # never the owner
    assert pick([good], done_keys={good["key"]}) == []                  # never twice
    assert pick([good], recent_authors={"asha rao"}) == []              # one comment per author per week
    assert pick([card(reactions=10)]) == []                             # no traction
    assert pick([card(comments=90)]) == []                             # thread too crowded
    assert pick([card(age_h=300)]) == []                                # too old
    assert pick([card(text="We are hiring analysts. " * 12)]) == []     # job ad
    assert pick([card(headline="Senior Talent Acquisition Partner")]) == []
    assert pick([good], banned=["Month End"]) == []


def test_pick_ranks_traction_first():
    a, b = card(key="a", reactions=40, author="A"), card(key="b", reactions=600, author="B")
    assert [c["key"] for c in pick([a, b])] == ["b", "a"]


def test_comment_problems():
    ok = "The refresh time drop is the real win here. Did you move the heavy joins into Power Query or keep them in DAX?"
    assert engage.comment_problems(ok, [], []) == []
    assert engage.comment_problems("Great post, thanks for sharing this with everyone today my friend.", [], [])
    assert engage.comment_problems("Nice.", [], [])
    assert engage.comment_problems(ok + " #powerbi", [], [])
    assert engage.comment_problems(ok, ["Power Query"], [])
    assert engage.comment_problems(ok, [], [ok])                        # repeats an earlier comment


def test_tidy_comment_removes_dashes_and_newlines():
    assert "\u2014" not in engage.tidy_comment("One idea \u2014 two\nlines")


def test_keywords_rotate_and_differ_per_run():
    assert len(set(engage.keywords_for(100, 0))) == 3
    assert engage.keywords_for(100, 0) != engage.keywords_for(100, 1)


def test_prompts_require_reading_and_judging():
    from linkedin_growth_mcp.strategy import commenting
    pr = commenting.build_prompt([card()], "facts", "voice", 2)
    assert "FIRST read each post fully" in pr and "post_point" in pr and "full_post" in pr
    assert "contradict" in commenting.judge_prompt([{"i": 0, "post": "p", "comment": "c"}])


def test_variety_rules():
    from linkedin_growth_mcp.strategy import commenting
    a = "The refresh time drop is the real win here. Which step gave you most of it, joins or the data model design?"
    b = "The refresh time gain looks big. Did you test it on the full history load or on a sample?"
    assert any("same opening" in w for w in engage.comment_problems(a, [], [a.replace("here", "there")]))
    assert any("question endings" in w for w in engage.comment_problems(b, [], ["Some earlier comment that ends with a question?"]))
    assert not any("question" in w for w in engage.comment_problems("Partitioning by month keeps incremental refresh fast, and it also makes backfills much safer for finance teams.", [], [a]))
    shapes = commenting.pick_shapes(3, [commenting.SHAPES[0], commenting.SHAPES[1]])
    assert len(set(shapes)) == 3 and commenting.SHAPES[0] not in shapes and commenting.SHAPES[1] not in shapes
    assert "required_shape" in commenting.build_prompt([card()], "f", "v", 1, shapes, [a])


def test_grounding_checks():
    post = "I rebuilt our month end report in Power BI and cut the refresh from 40 minutes to 4 using incremental refresh."
    good = "Incremental refresh is what makes month end reports in Power BI manageable. Did you partition by month?"
    assert engage.comment_problems(good, [], [], post) == []
    assert any("not grounded" in w for w in engage.comment_problems("Dashboards help teams decide faster when numbers stay consistent everyday.", [], [], post))
    assert any("figure not in the post" in w for w in engage.comment_problems(good + " We saw 73 percent savings.", [], [], post))
