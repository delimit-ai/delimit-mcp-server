"""Content Intelligence pipeline (LED-797).

Turns the tweet corpus into a daily topic radar for long-form content
(Reddit, HN, Dev.to, delimit.ai/blog). See Part 2 of
/home/delimit/delimit-private/specs/SOCIAL_PROMPT_V2_ARCHITECTURE.md.

Invariants (per LED-797 acceptance criteria):
- Every draft cites at least MIN_CITATIONS_PER_DRAFT tweet corpus rows
  verbatim with engagement counts.
- Every product claim is pulled from GROUND_TRUTH_FEATURES, never
  invented on the fly. Drafts that can't find a matching feature are
  suppressed instead of hallucinated.
- No auto-publishing. Every draft is written to disk and emailed to the
  founder for manual approval. The module never calls delimit_social_post
  or any posting endpoint.
- Daily digest files live under ~/.delimit/content/<channel>_<date>.md
  so the founder can diff them day over day.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from ai.tweet_corpus import TweetCorpus, DEFAULT_DELIMIT_KEYWORDS

logger = logging.getLogger("delimit.ai.content_intel")

CONTENT_DIR = Path.home() / ".delimit" / "content"
MIN_CITATIONS_PER_DRAFT = 3
DEFAULT_TOP_N_TOPICS = 5
DEFAULT_CLUSTER_HOURS = 72

# ── Delimit ground truth ─────────────────────────────────────────────
#
# Every product claim the pipeline emits must map to an entry below.
# Keep this list honest: only features that are actually shipped belong
# here. If a feature isn't listed, the drafter can't cite it.

GROUND_TRUTH_FEATURES: list[dict] = [
    {
        "feature": "OpenAPI breaking change detection",
        "keywords": ["openapi", "breaking change", "api governance", "semver", "diff", "spec"],
        "pitch": (
            "27 change types (17 breaking, 10 non-breaking), deterministic "
            "semver classification, zero-spec mode for FastAPI/NestJS/Express."
        ),
        "proof": "1,420+ gateway tests, GitHub Action v1.9.0, delimit-cli lint/diff.",
    },
    {
        "feature": "Cross-model context persistence",
        "keywords": [
            "claude code", "codex", "cursor", "gemini cli", "cross-model",
            "cross model", "context persistence", "memory", "session", "portability",
        ],
        "pitch": (
            "One workspace for every AI coding assistant. Memory, ledger, and "
            "soul files survive across Claude Code, Codex, Gemini CLI, and Cursor."
        ),
        "proof": "delimit_revive, delimit_soul_capture, delimit_session_handoff.",
    },
    {
        "feature": "Audit trail + evidence collection",
        "keywords": ["audit", "audit trail", "evidence", "siem", "splunk", "datadog", "compliance"],
        "pitch": (
            "Append-only hash-chained ledger plus SIEM streaming to "
            "Splunk/Datadog/EventBridge for every governance action."
        ),
        "proof": "delimit_ledger, delimit_evidence_collect, delimit_siem.",
    },
    {
        "feature": "MCP server for API governance",
        "keywords": ["mcp", "mcp server", "model context protocol", "agent harness"],
        "pitch": (
            "Public MCP server (180+ tools) covering govern, context, ship, "
            "observe, orchestrate domains. Works in any MCP-capable client."
        ),
        "proof": "delimit-cli npm package, Glama AAA score.",
    },
    {
        "feature": "GitHub Action for PR governance gates",
        "keywords": ["github action", "ci", "pull request", "pr", "ci gate"],
        "pitch": (
            "Drop-in PR comment action: green path shows 'safe for consumers', "
            "red path explains what breaks and how to fix it."
        ),
        "proof": "delimit-ai/delimit-action v1.9.0 on GitHub Marketplace.",
    },
    {
        "feature": "Multi-model deliberation",
        "keywords": ["deliberation", "consensus", "multi-model", "grok", "gemini", "claude", "codex"],
        "pitch": (
            "Debate mode routes the same question to Claude + Codex + Gemini + Grok "
            "and returns a consensus score with per-model rationale."
        ),
        "proof": "delimit_deliberate, DELIBERATION_* records in delimit-private/decisions.",
    },
]


# ── ContentIntelligence ──────────────────────────────────────────────


class ContentIntelligence:
    """Content radar over the tweet corpus.

    The pipeline is intentionally dumb/templated so it can run inside the
    build loop without LLM calls. Drafts are marked *pending founder
    approval* and the actual prose polish happens on the founder's side
    (or through a follow-up LLM pass that sees the cited evidence).
    """

    def __init__(
        self,
        corpus: TweetCorpus | None = None,
        content_dir: Path | None = None,
        ground_truth: list[dict] | None = None,
        notify: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.corpus = corpus or TweetCorpus()
        self.content_dir = Path(content_dir) if content_dir else CONTENT_DIR
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self.ground_truth = list(ground_truth) if ground_truth is not None else list(GROUND_TRUTH_FEATURES)
        self._notify = notify

    # ------------------------------------------------------------ public

    def generate_daily_digest(
        self,
        date: str | None = None,
        since_hours: int = DEFAULT_CLUSTER_HOURS,
        top_n: int = DEFAULT_TOP_N_TOPICS,
        email: bool = True,
    ) -> dict:
        """Run the full daily pipeline.

        Returns a dict with topics, draft file paths, and notification status.
        """
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        clusters = self.corpus.topic_cluster(since_hours=since_hours, min_cluster_size=3)
        if not clusters:
            logger.info("[content_intel] no clusters over %sh — skipping digest", since_hours)
            return {
                "date": date,
                "topics": [],
                "drafts": {},
                "digest_path": None,
                "notified": False,
                "skip_reason": "empty_corpus_window",
            }

        intersected = self.corpus.topic_intersect_delimit(clusters)
        ranked = self._rank_topics(intersected, top_n=top_n)
        if not ranked:
            logger.info("[content_intel] no topics intersected Delimit ground truth")
            return {
                "date": date,
                "topics": [],
                "drafts": {},
                "digest_path": None,
                "notified": False,
                "skip_reason": "no_delimit_intersection",
            }

        reddit_md = self._draft_reddit_targets_md(ranked)
        blog_md = self._draft_blog_topics_md(ranked)
        devto_md = self._draft_devto_topics_md(ranked)
        hn_md = self._draft_hn_topics_md(ranked)
        digest_md = self._draft_digest_md(ranked, date)

        files = {
            "reddit": self._write_channel_file(f"reddit_targets_{date}.md", reddit_md),
            "blog": self._write_channel_file(f"blog_topics_{date}.md", blog_md),
            "devto": self._write_channel_file(f"devto_topics_{date}.md", devto_md),
            "hn": self._write_channel_file(f"hn_topics_{date}.md", hn_md),
        }
        digest_path = self._write_channel_file(f"digest_{date}.md", digest_md)

        notified = False
        if email:
            notified = self._send_digest_email(date, ranked, files, digest_path)

        return {
            "date": date,
            "topics": [
                {
                    "keyword": t["keyword"],
                    "engagement": t["total_engagement"],
                    "matches": t.get("delimit_matches", []),
                    "cited_tweet_ids": [s["tweet_id"] for s in t["sample_tweets"][:MIN_CITATIONS_PER_DRAFT]],
                    "mapped_feature": t.get("_mapped_feature", {}).get("feature"),
                }
                for t in ranked
            ],
            "drafts": files,
            "digest_path": str(digest_path),
            "notified": notified,
        }

    def topic_probe(self, keyword: str, since_hours: int = 168) -> dict:
        """On-demand radar for a single keyword (delimit_content_intel_topic).

        Runs FTS over the corpus, clusters hits, intersects Delimit ground truth,
        and returns the ranked topics without writing any files or emails.
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return {"error": "keyword required", "topics": []}

        # Filter corpus cluster to only terms that contain the keyword.
        clusters = self.corpus.topic_cluster(
            since_hours=since_hours,
            min_cluster_size=2,
            keywords=[keyword],
        )
        intersected = self.corpus.topic_intersect_delimit(clusters)
        ranked = self._rank_topics(intersected, top_n=DEFAULT_TOP_N_TOPICS)
        return {
            "keyword": keyword,
            "since_hours": since_hours,
            "topics": [
                {
                    "keyword": t["keyword"],
                    "engagement": t["total_engagement"],
                    "matches": t.get("delimit_matches", []),
                    "sample_tweets": t["sample_tweets"][:3],
                    "mapped_feature": t.get("_mapped_feature", {}).get("feature"),
                }
                for t in ranked
            ],
        }

    def generate_weekly_summary(self, date: str | None = None) -> dict:
        """Weekly Monday rollup: 7-day clusters + which topics we already
        wrote drafts for vs. which we missed.
        """
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        clusters = self.corpus.topic_cluster(since_hours=168, min_cluster_size=5)
        intersected = self.corpus.topic_intersect_delimit(clusters)
        ranked = self._rank_topics(intersected, top_n=10)

        # Which daily digest files exist for the last 7 days?
        covered: list[str] = []
        missed: list[str] = []
        existing_digest_dates = {
            p.stem.replace("digest_", "")
            for p in self.content_dir.glob("digest_*.md")
        }
        for topic in ranked:
            kw = topic["keyword"]
            # A topic is "covered" if any daily digest in the last week mentioned it
            found = False
            for d in sorted(existing_digest_dates):
                digest_path = self.content_dir / f"digest_{d}.md"
                try:
                    if digest_path.exists() and kw in digest_path.read_text(encoding="utf-8").lower():
                        found = True
                        break
                except OSError:
                    continue
            (covered if found else missed).append(kw)

        summary_md = self._draft_weekly_summary_md(ranked, covered, missed, date)
        summary_path = self._write_channel_file(f"weekly_summary_{date}.md", summary_md)
        return {
            "date": date,
            "topics": [{"keyword": t["keyword"], "engagement": t["total_engagement"]} for t in ranked],
            "covered": covered,
            "missed": missed,
            "summary_path": str(summary_path),
        }

    # ------------------------------------------------------------ private

    def _rank_topics(self, intersected: list[dict], top_n: int) -> list[dict]:
        """Rank intersected clusters by engagement × relevance, tag each
        with the best-matching ground truth feature."""
        ranked: list[dict] = []
        for cluster in intersected:
            feature = self._best_feature(cluster)
            if feature is None:
                # Cluster intersected Delimit keywords in general but not a
                # specific shipped feature — suppress to avoid hallucination.
                continue
            # Require at least MIN_CITATIONS_PER_DRAFT sample tweets.
            if len(cluster.get("sample_tweets", [])) < MIN_CITATIONS_PER_DRAFT:
                continue
            cluster = dict(cluster)
            cluster["_mapped_feature"] = feature
            cluster["_relevance_score"] = len(set(cluster.get("delimit_matches", [])) & set(feature["keywords"]))
            ranked.append(cluster)

        ranked.sort(
            key=lambda c: (c["_relevance_score"], c["total_engagement"]),
            reverse=True,
        )
        return ranked[:top_n]

    def _best_feature(self, cluster: dict) -> dict | None:
        """Pick the ground-truth feature whose keyword set best matches the cluster."""
        cluster_tokens = {cluster.get("keyword", "").lower()}
        for s in cluster.get("sample_tweets", []):
            cluster_tokens.add((s.get("text") or "").lower())
        best: tuple[int, dict] | None = None
        for feature in self.ground_truth:
            score = 0
            for kw in feature["keywords"]:
                kw_l = kw.lower()
                if any(kw_l in t for t in cluster_tokens):
                    score += 1
            if score and (best is None or score > best[0]):
                best = (score, feature)
        return best[1] if best else None

    def _format_citation(self, sample: dict) -> str:
        text = (sample.get("text") or "").replace("\n", " ").strip()
        if len(text) > 220:
            text = text[:217] + "..."
        return (
            f'- [@{sample.get("author_handle", "?")}, '
            f'{sample.get("engagement", 0)} engagement, id={sample.get("tweet_id", "?")}] '
            f'"{text}"'
        )

    def _draft_reddit_targets_md(self, topics: list[dict]) -> str:
        lines = [
            "# Reddit targets — pending founder approval",
            "",
            "All targets below were surfaced by the LED-797 content intel pipeline",
            "from the tweet corpus. No drafts have been posted. Pick a target,",
            "open the linked thread, and either draft manually or route to",
            "`delimit_social_post`.",
            "",
        ]
        for i, t in enumerate(topics, 1):
            feature = t["_mapped_feature"]
            lines.append(f"## {i}. {t['keyword']}")
            lines.append("")
            lines.append(f"**Mapped feature:** {feature['feature']}")
            lines.append(f"**Delimit angle:** {feature['pitch']}")
            lines.append(f"**Proof points:** {feature['proof']}")
            lines.append(
                f"**Engagement in last {DEFAULT_CLUSTER_HOURS}h:** {t['total_engagement']} "
                f"({t['count']} tweets)"
            )
            lines.append("")
            lines.append(f"**Suggested subreddits:** r/programming, r/devops, r/webdev")
            lines.append("")
            lines.append("**Evidence (corpus rows cited):**")
            for s in t["sample_tweets"][:MIN_CITATIONS_PER_DRAFT]:
                lines.append(self._format_citation(s))
            lines.append("")
            lines.append("**Draft seed (founder to polish before posting):**")
            lines.append("```")
            lines.append(self._seed_reddit_reply(t, feature))
            lines.append("```")
            lines.append("")
        return "\n".join(lines) + "\n"

    def _draft_blog_topics_md(self, topics: list[dict]) -> str:
        lines = [
            "# delimit.ai/blog topic candidates — pending founder approval",
            "",
            "Long-form angles grounded in the last 72h of corpus signal.",
            "Each entry is a seed: skeleton outline, ground-truth-only claims,",
            "and at least three cited tweet rows as evidence.",
            "",
        ]
        for i, t in enumerate(topics, 1):
            feature = t["_mapped_feature"]
            lines.append(f"## {i}. {self._blog_title_for(t, feature)}")
            lines.append("")
            lines.append(f"**Topic cluster:** `{t['keyword']}` ({t['total_engagement']} engagement)")
            lines.append(f"**Mapped feature:** {feature['feature']}")
            lines.append("")
            lines.append("**Outline (founder to expand to 800-1200 words):**")
            lines.append("")
            lines.append(f"1. Hook — the pain visible in the corpus this week (see citations)")
            lines.append(f"2. Why existing tools fall short")
            lines.append(f"3. How {feature['feature']} addresses it: {feature['pitch']}")
            lines.append(f"4. Proof: {feature['proof']}")
            lines.append(f"5. CTA — link to docs / GitHub Action / npm install")
            lines.append("")
            lines.append("**Corpus evidence (minimum 3 citations required — present here):**")
            for s in t["sample_tweets"][:MIN_CITATIONS_PER_DRAFT]:
                lines.append(self._format_citation(s))
            lines.append("")
        return "\n".join(lines) + "\n"

    def _draft_devto_topics_md(self, topics: list[dict]) -> str:
        lines = [
            "# Dev.to tutorial candidates — pending founder approval",
            "",
            "Dev.to audience expects hands-on walkthroughs (1000-1500 words).",
            "Every candidate below has a working ground truth feature behind it.",
            "",
        ]
        for i, t in enumerate(topics, 1):
            feature = t["_mapped_feature"]
            lines.append(f"## {i}. Tutorial: {feature['feature']} — {t['keyword']}")
            lines.append("")
            lines.append("**Shape:** problem → walkthrough → Delimit hook → runnable example")
            lines.append("")
            lines.append(f"**Delimit pitch:** {feature['pitch']}")
            lines.append(f"**Proof:** {feature['proof']}")
            lines.append("")
            lines.append("**Evidence:**")
            for s in t["sample_tweets"][:MIN_CITATIONS_PER_DRAFT]:
                lines.append(self._format_citation(s))
            lines.append("")
        return "\n".join(lines) + "\n"

    def _draft_hn_topics_md(self, topics: list[dict]) -> str:
        lines = [
            "# Hacker News submission candidates — pending founder approval",
            "",
            "HN rewards substance over hype. Titles stay <70 chars, no adjectives.",
            "",
        ]
        for i, t in enumerate(topics, 1):
            feature = t["_mapped_feature"]
            title = self._hn_title_for(t, feature)
            lines.append(f"## {i}. {title}")
            lines.append("")
            lines.append(f"**Title ({len(title)} chars):** {title}")
            lines.append(f"**Self-text angle:** {feature['pitch']}")
            lines.append(f"**Proof to include:** {feature['proof']}")
            lines.append("")
            lines.append("**Corpus evidence:**")
            for s in t["sample_tweets"][:MIN_CITATIONS_PER_DRAFT]:
                lines.append(self._format_citation(s))
            lines.append("")
        return "\n".join(lines) + "\n"

    def _draft_digest_md(self, topics: list[dict], date: str) -> str:
        lines = [
            f"# Content intel daily digest — {date}",
            "",
            f"Pipeline ran over the last {DEFAULT_CLUSTER_HOURS}h of corpus rows.",
            f"Top {len(topics)} topics that intersect Delimit ground truth:",
            "",
        ]
        for i, t in enumerate(topics, 1):
            feature = t["_mapped_feature"]
            lines.append(f"{i}. **{t['keyword']}** → {feature['feature']} "
                         f"({t['total_engagement']} engagement, {t['count']} tweets)")
        lines.append("")
        lines.append("## Channels drafted")
        lines.append("")
        lines.append(f"- Reddit targets: `reddit_targets_{date}.md`")
        lines.append(f"- Blog topics: `blog_topics_{date}.md`")
        lines.append(f"- Dev.to tutorials: `devto_topics_{date}.md`")
        lines.append(f"- HN submissions: `hn_topics_{date}.md`")
        lines.append("")
        lines.append("All drafts require founder approval before publishing. No auto-post.")
        lines.append("")
        return "\n".join(lines) + "\n"

    def _draft_weekly_summary_md(
        self, topics: list[dict], covered: list[str], missed: list[str], date: str
    ) -> str:
        lines = [
            f"# Content intel weekly summary — week of {date}",
            "",
            f"Top 7-day topics intersecting Delimit ground truth:",
            "",
        ]
        for i, t in enumerate(topics, 1):
            lines.append(f"{i}. `{t['keyword']}` — {t['total_engagement']} engagement")
        lines.append("")
        lines.append("## Covered (already in a daily digest)")
        for c in covered or ["(none)"]:
            lines.append(f"- {c}")
        lines.append("")
        lines.append("## Missed (no daily digest mention)")
        for m in missed or ["(none)"]:
            lines.append(f"- {m}")
        lines.append("")
        return "\n".join(lines) + "\n"

    def _blog_title_for(self, topic: dict, feature: dict) -> str:
        return f"What the corpus is telling us about {topic['keyword']}"

    def _hn_title_for(self, topic: dict, feature: dict) -> str:
        title = f"Delimit: {feature['feature']} — answer to {topic['keyword']}"
        return title[:70]

    def _seed_reddit_reply(self, topic: dict, feature: dict) -> str:
        # Intentionally short/neutral — founder polishes before posting.
        sample = topic["sample_tweets"][0]
        return (
            f"Saw the {topic['keyword']} discussion picking up again. "
            f"Been working on this exact problem — {feature['feature']}. "
            f"Happy to share what we learned if useful."
        )

    def _write_channel_file(self, name: str, content: str) -> Path:
        path = self.content_dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def _send_digest_email(
        self, date: str, topics: list[dict], files: dict, digest_path: Path
    ) -> bool:
        """Email the digest to the founder via delimit_notify.

        Never auto-posts. The email lists topic summaries + paths to the
        per-channel draft files so the founder can review before acting.
        """
        subject = f"[CONTENT INTEL] Daily digest {date} — {len(topics)} topics"
        body_lines = [
            f"Content intel daily digest for {date}.",
            "",
            f"Top {len(topics)} topics (ranked by engagement × ground truth relevance):",
            "",
        ]
        for i, t in enumerate(topics, 1):
            feature = t["_mapped_feature"]
            body_lines.append(
                f"  {i}. {t['keyword']} → {feature['feature']} "
                f"({t['total_engagement']} engagement, {t['count']} tweets)"
            )
        body_lines += [
            "",
            "Draft files (require your approval before publishing):",
            f"  - Reddit targets:   {files['reddit']}",
            f"  - Blog topics:      {files['blog']}",
            f"  - Dev.to tutorials: {files['devto']}",
            f"  - HN submissions:   {files['hn']}",
            f"  - Digest overview:  {digest_path}",
            "",
            "Every draft cites 3+ corpus rows verbatim. No auto-posting.",
            "Reply 'approved <channel> <n>' to route a draft to delimit_social_post.",
        ]
        body = "\n".join(body_lines)

        notify_fn = self._notify
        if notify_fn is None:
            try:
                from ai.notify import send_notification as _sn
                notify_fn = _sn
            except Exception as e:
                logger.warning("[content_intel] notify import failed: %s", e)
                return False
        try:
            notify_fn(
                channel="email",
                subject=subject,
                message=body,
                event_type="content_intel_daily_digest",
            )
            return True
        except Exception as e:
            logger.warning("[content_intel] send_notification failed: %s", e)
            return False
