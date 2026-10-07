# map.json schema (v1)

The agent writes this file; `pmap.py render` turns it into the page. `pmap.py validate` checks it.
Statuses: parts / remaining / chain nodes use `done | in_progress | not_started | stuck`;
milestones use `done | current | future` (exactly one `current`).

```jsonc
{
  "project": { "name": "keepline", "summary": "one line: what the project is" },
  "headline": "Ledger merged; main CI is red on one test that PR #140 fixes; #140 needs your review.",
  "style": { "lang": "zh" },                       // optional; theme/accent come from `pmap.py style`

  "milestones": [                                   // left → right, past to future
    { "name": "v1.1.4", "status": "done", "date": "2026-09-26", "note": "notarized" },
    { "name": "Ledger release", "status": "current", "proposed": true },   // proposed = not defined in the repo
    { "name": "M0", "status": "future", "note": "baseline" }
  ],
  "remaining": [                                    // what is left before the current milestone
    { "label": "Merge PR #140", "status": "in_progress", "note": "CI green", "part": "ledger" },
    { "label": "main CI green", "status": "stuck" }
  ],

  "chain": {                                        // blocker chain toward the current milestone
    "nodes": [                                      // ORDER = the main path, drawn left → right
      { "id": "pr140", "label": "Merge PR #140", "sub": "awaiting review", "status": "in_progress", "bottleneck": true, "part": "ledger" },
      { "id": "hold", "label": "Hold the release", "status": "not_started", "if": { "decision": "ship", "option": 1 } },
      { "id": "ci", "label": "main CI green", "sub": "1 failing test", "status": "stuck" }
    ],
    "edges": [                                      // edges between neighbours become arrows; others are listed as "other dependencies"
      { "from": "pr140", "to": "ci", "kind": "evidence", "why": "#140 fixes the only failing test (PR body)" },
      { "from": "ci", "to": "tag", "kind": "inferred", "why": "release.yml does not run tests; suggested order" }
    ]
  },

  "parts": [
    {
      "id": "ledger",                               // stable across runs: the diff matches on it
      "name": "Progress ledger",
      "status": "in_progress",
      "summary": "what this part is and where it stands",
      "progress": { "done": 15, "total": 15, "label": "code tasks" },   // only with a real count
      "open": "not merged; manual check of notifications left",         // what is left when the count is full but it isn't done
      "waiting_on": "required when status is stuck",
      "evidence": ["#139", "e6cef9e", "`specs/progress-ledger/tasks.md`"],  // or "inferred": true
      "paths": ["src/services/ledger", "src/ui/LedgerPage.tsx"]          // dir/file prefixes or globs (src/**/*.ts); lets the next collect map changed files to this part
    }
  ],

  "next_step": {
    "title": "Review and merge PR #140",
    "owner": "user",                                // user | agent: who does it
    "action": "https://github.com/o/r/pull/140",    // the command, link or concrete step
    "why": "it fixes the only failing test on main",
    "unblocks": "main CI green, then the round-5 PR"
  },
  "decisions": [
    { "id": "ship", "question": "Ship round 5 as default?", "default": "yes, as in round 5",
      "options": ["yes, as in round 5", "hold for the failing test"],
      "deadline": "10-08 10:00", "reversible": true, "proposed": true },
    { "question": "Merge #140 without review?", "default": "wait for review" }   // no deadline: waits for the user
  ],
  "uncertain": ["things you could not verify; inferences that affect the conclusion"],

  "panels": [                                       // project-specific detail, collapsed at the bottom
    { "title": "Ledger tasks T1–T15", "hint": "specs/progress-ledger/tasks.md", "kind": "table",
      "columns": ["Task", "Status"], "rows": [["T1", "done"]] },
    { "title": "Open PRs", "kind": "list", "items": ["#140 ..."] },
    { "title": "Note", "kind": "text", "text": "..." }
  ],
  "sources": ["README", "CHANGELOG", "git log", "gh pr/issue/run"]
}
```

Text fields support `` `code` ``. When the repo has a GitHub origin, `#123` becomes a link and
short SHAs that appear in facts.json commits become commit links.

Validation rules worth knowing:
- every part needs `evidence[]` or `"inferred": true`; `stuck` needs `waiting_on`
- evidence edges need `why`
- `progress.total` > 0; omit `progress` when there is no real count (no made-up percentages)
- part `id`s are unique and should stay the same across runs
- `paths` entries match whole path segments: `src` covers `src/a.ts` but not `src-tauri/x.rs`. In a glob,
  `*` stays inside one directory and `**` crosses directories: `src/services/*.ts` does not cover
  `src/services/ledger/a.ts`, `src/**/*.ts` does
- `render` also prints `warning:` lines (bottleneck, edge direction, overlapping paths, evidence format,
  done vs progress, text length). They never block; fix them unless you have a reason
- `part` on a chain node or remaining item is a part id. The page uses it to light the same work everywhere you click
- `decisions[].options` lists real choices. `default` must be one of them. `id` is required only when a chain node's `if` points at the decision
- `if.option` is the index into that decision's `options`. Nodes without `if` stay on every path. The page draws the default path, and the other path when asked
- `deadline` means "if nobody answers, the default goes ahead". Only reversible, local choices may have one, so a
  deadline requires `"reversible": true`. Anything outward-facing or hard to undo (push, publish, merge, delete,
  spend) gets no deadline and shows as waiting for the user
- `deadline` may be an ISO timestamp, `MM-DD HH:MM`, or `HH:MM` (on the map's date). The page counts it down and marks the default once it has passed

The page also filters parts, collapses unchanged done cards, peeks evidence without leaving, copies a decision as text to paste back to the agent, and scrubs the `history/` snapshots. None of that needs extra fields.
