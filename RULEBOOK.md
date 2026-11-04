# LinkedIn Growth Rule Book (v2)

Goal: grow reach and followers on one real account until the profile earns recognition (profile badges, creator features, inbound opportunities). No one can promise a specific award, so the rules target what drives them: consistent, useful, original posts that people comment on.

## A. Hard limits (never broken)
1. Own account only, official LinkedIn API only. No engagement pods, bought engagement, auto-comments, auto-DMs, auto-connects or scraping other people. These risk the account, and the account is the asset.
2. IPO numbers must come from verified facts. Work-story posts cannot be fact-checked; they auto-post by the owner's choice, so keep the profile and backlog briefs accurate.
3. IPO posts are educational only: no buy/sell/apply/avoid wording, disclaimer on every one.
4. No employer-confidential detail. No links or @mentions in the post body.
5. No em or en dashes anywhere.
6. One post a weekday (5 a week), none on weekends or listed holidays. Quality beats volume.

## B. What a post must have
1. **Hook in the first 210 characters.** Feeds cut the text there. Plain English first, never a bare number or currency. Exception (tested arm, non-IPO posts only, from 2026-11-04): the `number` hook may open with `<count> <thing>` when the count comes from the story bank facts; the hook_report `number_led` row is the evidence. IPO posts keep the ban.
2. **One idea, short paragraphs**, one line per point, at most 25 words per sentence.
3. **One specific question at the end.** Comments are the strongest reach signal and today every post has 0.
4. **Something saveable**: a comparison, a checklist or a number people want to keep.
5. **Hashtags: 3 to 4, relevant, mixed size.** Non-IPO posts: auto-picked from the post's content (1 broad, up to 2 mid, up to 2 niche) and re-ranked by measured reach once a tag has 3 or more measured posts. IPO posts: series tag plus IPO reach tags. Never IPO tags on non-IPO posts.
6. **Image or carousel** on the best posts once the text-only test ends (see E).

## C. Content mix (per 7 days)
| Share | Pillar | Why |
|---|---|---|
| About 3 | IPO data posts (open IPOs) | The niche that makes the account distinctive |
| 1 | Saturday listing scorecard | Saveable, recurring, builds a habit |
| About 2 | Work and automation stories (data, finance ops, Power BI, AI for analysts) | Shows who you are; explains the profile |
| 1 | Learning in public (this project, what failed) | Builds trust and a story people follow |

On a day with no IPO or scorecard post, the autopilot drafts one backlog post, runs form and safety gates (placeholders, links, advice wording, length, dashes) and publishes it automatically. The owner accepted that work-story claims cannot be fact-checked.

## D. Daily and weekly routine (the part software cannot do for you)
1. **First hour after posting:** reply to every comment. This is the single biggest lever and is done by you, in your voice. Auto-replies are deliberately not built.
2. **10 minutes a day:** leave 3 thoughtful comments on posts from IPO, data and finance people. Real comments, not "great post".
3. **Weekly:** ask Claude for `analyze_performance`, then adjust.
4. **Profile:** headline states who you help and how (for example "Data analyst | IPO breakdowns in plain English"), featured section pins your best post, banner matches the niche.

## E. Experiments and decision rules
- Hold the current IPO format unchanged until **2026-11-04** (your decision). Then the autopilot runs one trial by itself (switch off with `AUTO_EXPERIMENTS=false`). Images stay off by your choice.
- **Weekdays only.** No weekend posts, and no posts on listed low-activity holidays (10 Nov, 24 Nov, 25 Dec 2026; add more with the `skip_dates` setting). Weekend and holiday posts are noise in any comparison.
- **One trial, two questions at once.** Every post day is assigned a cell from a balanced rotation: short or long format, and a 9:00, 13:00 or 18:00 IST slot. Both arms run side by side through the same weeks, so account growth and news days hit both equally. Format and slot are crossed evenly, so they do not confuse each other. (This replaces the old "one change at a time, before vs after" rule: running side by side is what removes the confusion that rule was protecting against.)
- **Fair reading.** Every post is read at its 72h checkpoint. Impressions are compared on a log scale (one viral post cannot decide it) and adjusted for post kind (IPO posts against IPO posts, other posts against other posts).
- **Verdict.** Short format: first look at 5 posts per arm, final call at 10. Keep it only if it is at least 15% better and a permutation test gives p of 0.10 or less; clearly worse (-20% or more) stops early; unclear at 10 goes back to the long format. Slot: first look at 6 posts per slot, final call at 8. Pin a slot only if clearly better (p of 0.05 or less, since the best of three is being picked, and at least +15%); otherwise 9:00 stays. Each factor is frozen the moment its verdict is final, and the trial is archived when both are.
- **Why those hours.** The audience is Indian, senior, at large firms (Bengaluru 22%, Mumbai 14%, Delhi 11%). India-specific studies (MagicPost 220k posts, Reactin 50k) point to weekday mornings around 8 to 11 IST, a lunch peak around 13:00 and an evening B2B slot around 18:00 to 20:00. US studies (Buffer, Sprout) favour afternoons in US time and are not used. The studies disagree and are mostly vendor content, so the choice is made on this account's own numbers.
- Every result is logged (`experiment_result`) and archived, never deleted. A tag stays in rotation unless its measured average falls below half the overall average. Comment feedback becomes writing rules automatically (max 25, retired with `retire_feedback_rule`).
- Honest limit: at one post per weekday the trial takes about four to five weeks. Small effects will come back as "no clear difference" and the old setup stays.

## F. Scorecard
Track weekly: impressions per post, comments per post, followers gained, saves. Targets, tied to the 227 impressions baseline:
- Weeks 1 to 4: 400 impressions per post, 1 or more comment per post.
- Weeks 5 to 8: 800 per post, 3 or more comments per post.
- Month 3: 1,500 per post, steady follower growth.
These are targets, not predictions. Adjust after real data.

## G. Reliability rules
1. Server starts at login (Windows task `LinkedInGrowthMCP`); a late start still posts after 9:00.
2. Desktop alerts for a missed post, errors, expiring token (7 days) and factual-error comments.
3. Gemini free tier: 16 calls a day cap, 6 reserved for posting.
4. Append-only data: nothing is deleted, so the dashboard and ML can use it later.

## Review of this rule book (what was wrong in v1, now fixed)
- v1 posted nothing on days with no open IPO. Fixed: off-day draft for approval (C).
- v1 added IPO hashtags to every post. Fixed: hashtags depend on post type and learn from results (B5).
- v1 had no comment or reply routine, though comments drive reach. Added D1 and D2.
- v1 targets were vague. Added numeric targets and keep/revert rules (E, F).
- Risk noted: dense 1,600 to 2,300 character IPO posts may suit searchers but get few comments. This is the first thing to test after 11-04.

## H. Topic rollout (added 2026-10-30)
Auto-posting is limited to: IPO series, scorecard, `story` (your work stories) and `bi_features` (Power BI and Excel). `product` and `ai` ideas are never posted. To add a topic, set `PILLAR_SERIES` in `.env` (for example `story,bi_features,product`). Add one topic at a time, after at least 5 posts of the current set have been measured and the new topic is judged against their average impressions and comments.

Automatic rollout: the autopilot unlocks the next topic itself, in the order `product`, then `ai`. It does so when at least 5 posts from the current non-IPO topics, published since the last unlock, have been measured and their mean score beats the bar. The bar is not fixed: it is the mean score of every post measured before the test began (kept between 0.4 and 0.7, 0.5 until such posts exist), so it rises as your results improve. Otherwise the topic stays locked and the reason is logged (`topic_hold`). Each unlock is logged (`topic_unlocked`) and raises a desktop alert.

Draft clean-up: a draft waiting for approval is deleted completely 6 hours after it was made, once the day's post has gone out (or when it is from an earlier day). Its backlog idea goes back to `backlog`. Published posts and their metrics are never deleted.

Draft timing: nothing is drafted ahead of time. The day's IPO post is drafted and checked 30 minutes before the posting hour (8:30 for a 9:00 slot) and published at 9:00 exactly as drafted, so a failed draft has time to retry. Other posts are drafted and published in the same cycle.

## I. Bug fixes 2026-10-30 (keep these invariants)
1. A post with no verified facts pack must never be asked for `[TBD]` placeholders: the publish gate rejects them, so the post would simply never go out. `draft_post(no_placeholders=True)` is used whenever an idea has no facts.
2. The hashtag picker, not the drafting model, decides the tags on every non-IPO post. The model's own tags are stripped and only used to top up when the picker chose fewer than 4. Without this the picker was dead code and `post_tags` stayed empty, so the tag learning loop could never start.
3. A draft behind a published post is never deleted: `list_posts()` inner-joins drafts, so losing the draft row would erase the post from analytics, scoring and metrics checkpoints.

## J. Growth features added 2026-10-30
1. **Conversation quality tracking.** Replies are written by you, by hand: the system never drafts or sends one. Every cycle it checks the newest post up to 36 hours old, at most once every 2 hours, and when the page has changed it scores the exchange: how substantive the comments are (question, insight, praise, criticism, low effort, spam), how many you answered, and how good your own replies were, with up to 3 coaching points. An alert fires when a comment is still unanswered. Tools: `conversation_report`, `check_comments_now`.
2. **Follower tracking.** One reading a day from the profile page, stored append-only. Impressions alone cannot tell reach growth from audience growth. Tools: `track_followers`, `follower_history`.
3. **Posting-time learning.** `POST_HOURS` lists the candidate hours. With one hour (the default, 9) behaviour is unchanged and nothing is learned. With several, a Thompson-sampling bandit picks one hour per day from how past posts at each hour scored, and the choice is fixed for the whole day. Tool: `posting_time_report`.
4. **Editor gate.** A draft that passes the deterministic gates is scored out of 10 by the Gemini editor. Below `EDITOR_MIN_SCORE` (6) it is rewritten once using the editor's own weaknesses as hints, then the better-scoring version is published. The editor never blocks a post outright: a weak post beats no post. Switch off with `EDITOR_GATE=false`.

Quota note: the editor costs up to 2 extra Gemini requests a day and the reply drafting 1, against the free-tier cap of 16. If the cap starts biting, turn off `EDITOR_GATE` first.
