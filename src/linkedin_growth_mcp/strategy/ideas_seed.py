"""Starter content backlog. Seeded once into the DB; edit via the idea tools afterwards.

Each tuple: (series, title, brief, priority 1=high..3=low)
"""
from __future__ import annotations

SEED_IDEAS: list[tuple[str, str, str, int]] = [
    # --- IPO series: repeat these six steps for every IPO worth covering ---
    ("ipo", "IPO step 1 - poll: which peers should I compare?",
     "Posted ~1 day before the IPO opens. Say you plan a comparison for <company>. Propose 3-4 peers chosen by "
     "similar revenue band and growth rate, and ask followers to vote/comment on which to include. "
     "If several IPOs are open back to back, ask which ones people want compared.", 1),
    ("ipo", "IPO step 2 - DRHP breakdown",
     "Walk through the company's DRHP/RHP: business model, 3-year revenue/profit/margin trend, objects of the issue "
     "(fresh issue vs offer for sale), promoter holding, top risk factors. Use only numbers supplied in FACTS.", 1),
    ("ipo", "IPO step 3 - peer-to-peer comparison",
     "Compare the IPO company with the peers chosen by followers (or by revenue band + growth): revenue, growth, "
     "EBITDA/PAT margin, ROE/ROCE, valuation multiples (P/E, EV/Sales). End with where the IPO price sits vs peers. "
     "Educational analysis, no buy/sell call.", 1),
    ("ipo", "IPO step 4 - multi-IPO shootout",
     "When several IPOs run in a row, compare all of them on the metrics followers voted for. One ranked table plus "
     "what each does best/worst. No recommendation.", 2),
    ("ipo", "IPO step 5 - daily GMP tracker",
     "Daily update of grey market premium (GMP) during the IPO window, plus subscription numbers. Label GMP clearly "
     "as unofficial and unreliable; cite the source and date for every number.", 2),
    ("ipo", "IPO step 6 - GMP vs listing recap",
     "After listing: chart each day's GMP against the actual listing price and day-1 close. What did GMP get right "
     "or wrong, and what did the DRHP-based comparison miss? Keep a running scoreboard across IPOs.", 1),
    # --- Excel / Power BI / BI tools ---
    ("bi_features", "One Power BI feature, one use case",
     "Pick a new or little-known Power BI feature (DAX trick, Copilot, field parameters, calculation groups). Show a "
     "before/after and a business use case.", 1),
    ("bi_features", "One Excel feature, one use case",
     "A new or underused Excel feature (dynamic arrays, LET/LAMBDA, Power Query, Copilot). Before/after on a real "
     "finance or ops task.", 1),
    ("bi_features", "Power BI vs Looker Studio: same dashboard in both",
     "Build the same dashboard in both tools and say where each wins (data modelling, sharing, cost, speed).", 2),
    ("bi_features", "Dashboard teardown",
     "Take a public dashboard and point out 3 things to fix (chart choice, clutter, misleading axis).", 3),
    # --- Product analysis ---
    ("product", "Metric teardown of a well-known app",
     "Pick a familiar product (food delivery, UPI, broker app). Define its north-star metric and 3 supporting "
     "metrics, and how you would measure each.", 1),
    ("product", "First-week questions as a new analyst",
     "The first 5 data questions to ask when joining a product team.", 2),
    ("product", "A/B test explained with a common mistake",
     "A simple example and the way people misread the result (peeking, low sample, novelty effect).", 3),
    # --- Own story / proof of work ---
    ("story", "Pipeline from 24 hours to 10 minutes",
     "The optimisation story from your analytics work: what was slow, what you changed, the business impact. "
     "Only after you confirm what is safe to share publicly.", 1),
    ("story", "Stock-averaging Excel model: what v1 got wrong",
     "Your featured model: the idea, the mistake you found, what changed in v2.", 2),
    ("story", "Daily analyst routine",
     "A light behind-the-scenes post about how you structure a day of analysis. Keeps the profile human.", 3),
    # --- AI for analysts ---
    ("ai", "Claude / MCP / agentic AI for analysts - what I built this week",
     "A learning-in-public post about a real experiment (for example this LinkedIn MCP project): problem, what "
     "worked, what failed.", 1),
    ("ai", "Gemini API for financial reporting",
     "How an LLM can speed up validation or reporting checks, with no employer-confidential details.", 2),
    ("ai", "New Claude feature and what it changes for analysts",
     "React to a fresh Claude update with a concrete analyst use case you actually tried.", 2),
]
