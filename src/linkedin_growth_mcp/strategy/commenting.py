"""Prompts for the comments the autopilot leaves on other people's posts: read first, then write, then a second judge."""
from __future__ import annotations

import json
from typing import Any

RULES = (
    "Write like a working analyst leaving a useful comment, in plain simple English, first person. "
    "15 to 50 words, one to three short sentences, no line breaks. "
    "The comment must prove the post was read: build it on ONE specific detail from the post (a number, a step, a tool, "
    "a claim, a trade-off) and add something a stranger could not have written without reading it: a concrete practical "
    "example, a caveat from real practice, or ONE narrow question about that detail that the author can answer. "
    "Be warm and constructive. Never argue against the post's main point or imply the author is wrong; extend it instead. "
    "Never open with praise or thanks (no 'Great post', 'Love this', 'Insightful', 'Thanks for sharing', 'Well said'). "
    "No vague claims like 'significantly reduces time' or 'is a game changer'. "
    "No emojis, hashtags, links, mentions, dashes, or sales talk. No 'As a ...'. Do not just agree. "
    "Do not claim any personal experience, result, employer or number that is not in OWNER FACTS; if nothing there fits, "
    "write from general professional knowledge without claiming it happened to you. "
    "If the post is off topic for a business analyst working in Power BI, Looker Studio, Excel, finance or data, is a "
    "personal tragedy or news, politics, religion, a job ad or an ad, a plain list or carousel with no argument to build on, "
    "or you cannot add real value, leave it out."
)


# Each comment is assigned a different shape, so they do not all read "statement, then a do-you-X-or-Y question".
SHAPES = (
    "a short practical tip that extends the post's detail (no question)",
    "an edge case or caveat that the post's detail runs into in real work (no question)",
    "one narrow question about the post's detail, with no statement before it",
    "a concrete mini example of the post's idea on a different kind of data (no question)",
    "a one-sentence reframe of the post's point in your own words, then one small addition (no question)",
    "a short contrast: what usually goes wrong before this works, in two sentences (no question)",
    "a two-sentence 'if you also need X, try Y' suggestion that fits the post (no question)",
)


def pick_shapes(n: int, used_recently: list[str]) -> list[str]:
    """n distinct shapes, least recently used first."""
    order = sorted(range(len(SHAPES)), key=lambda i: (used_recently[::-1].index(SHAPES[i]) if SHAPES[i] in used_recently else 99), reverse=True)
    return [SHAPES[i] for i in order[:n]]


def build_prompt(cands: list[dict[str, Any]], owner_facts: str, voice: str, how_many: int,
                 shapes: list[str] | None = None, recent: list[str] | None = None) -> str:
    posts = [{"i": i, "author": c["author"], "author_headline": c["headline"], "reactions": c["reactions"],
              "comments_so_far": c["comments"], "full_post": c["text"][:2500],
              "required_shape": (shapes or [""] * len(cands))[i % max(len(shapes or [""]), 1)]} for i, c in enumerate(cands)]
    return "\n".join([
        f"You write LinkedIn comments for a business analyst. FIRST read each post fully. Pick at most {how_many} posts "
        "where you can add real value, and for each one fill the fields in this order: what the post argues, the one detail "
        "you will build on, then the comment.",
        "RULES: " + RULES,
        "Each post has a required_shape: write that shape, and make the comments clearly different from each other in length "
        "(somewhere between 15 and 50 words), first words and sentence pattern. Do not end every comment with a question.",
        "RECENT COMMENTS ALREADY POSTED (never reuse their first words, sentence pattern or closing; sound like a different "
        "moment of thought): " + json.dumps((recent or [])[-8:], ensure_ascii=False),
        "OWNER FACTS (the only personal facts you may use): " + (owner_facts[:1500] or "none"),
        "VOICE NOTES: " + (voice[:500] or "plain, specific, no hype"),
        "POSTS (JSON): " + json.dumps(posts, ensure_ascii=False),
        'Return JSON only: {"comments":[{"i":<post index>,"post_point":"<one sentence: what the post says>",'
        '"detail":"<the specific detail quoted or paraphrased from the post>","comment":"<text>"}]}. '
        'Return {"comments":[]} if none deserve a comment.',
    ])


def judge_prompt(items: list[dict[str, Any]]) -> str:
    """items: [{i, post, comment}]. A strict second reader that rejects comments a stranger could have written unread."""
    return "\n".join([
        "You are a strict editor checking LinkedIn comments before they are posted under a real person's name.",
        "For each item decide keep=true only if ALL hold: (1) it uses a specific detail that is really in the post; "
        "(2) it adds a concrete example, caveat or narrow question, not a generic statement; (3) it does not contradict or "
        "belittle the author; (4) every factual claim is true or general knowledge, with no invented personal experience; "
        "(5) it would not look like spam or AI filler to the author; (6) the author would find it genuinely useful or interesting, "
        "not obvious or filler. Give each a score from 1 to 10 for how good it is as a public comment; 8 or more only for "
        "comments you would be happy to see under your own name.",
        "ITEMS (JSON): " + json.dumps(items, ensure_ascii=False),
        'Return JSON only: {"verdicts":[{"i":<index>,"score":<1-10>,"keep":true|false,"reason":"<short>"}]}.',
    ])
