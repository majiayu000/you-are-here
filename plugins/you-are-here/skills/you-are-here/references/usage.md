# Optional token usage

The map stays focused on progress, remaining work and the next step. When usage is requested,
show only “原始会话” and “本轮 Skill 使用” totals in a compact row; token details and source scope
are collapsed by default. Do not add deep conversation reviews, stage analysis, execution timelines
or cross-run comparisons as part of routine mapping.

Use the script to count usage; do not reread a whole conversation with the model just to calculate
numbers. For a Claude source session and a Codex Skill invocation:

```sh
python3 <skill-dir>/scripts/session_stats.py --source /path/to/claude-session.jsonl \
  --run /path/to/codex-rollout.jsonl --turn <skill-invocation-turn-id> \
  --out <output-dir>/analysis.json
```

Use exact source paths and the invocation's `task_started` / `task_complete` boundary, not the whole
hosting thread. Count the full invocation, including reading, analysis, rendering and verification;
Skill development is a separate scope. Reuse the saved original-session statistics when only the
map presentation changes. `render` reads `analysis.json` without accessing conversation logs.
Usage belongs to the invocation whose time interval contains the map's `collected_at`. The compact
row shows that invocation's date. A new collection outside that interval shows current usage as
unavailable, retaining the prior dated record only in details; recollect usage when requested.

The reader includes retained Claude subagent logs, deduplicates message/response IDs, and excludes
cumulative counters. Cache reads are included in totals; reasoning is part of output. Missing usage
is unavailable, not zero or estimated billing. Ongoing usage is a snapshot, not a final total.
