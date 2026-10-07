# map.json schema (v1)

The agent writes this file; `pmap.py render` turns it into the page. `pmap.py validate` checks it.
Statuses: parts / remaining / chain nodes use `done | in_progress | not_started | stuck`;
milestones use `done | current | future` (exactly one `current`).

```jsonc
{
  "project": { "name": "keepline", "summary": "one line: what the project is" },
  "style": { "lang": "zh" },                       // optional; theme/accent come from `pmap.py style`

  "milestones": [                                   // left → right, past to future
    { "name": "v1.1.4", "status": "done", "date": "2026-09-26", "note": "notarized" },
    { "name": "Ledger release", "status": "current", "proposed": true },   // proposed = not defined in the repo
    { "name": "M0", "status": "future", "note": "baseline" }
  ],
  "remaining": [                                    // what is left before the current milestone
    { "label": "Merge PR #140", "status": "in_progress", "note": "CI green" },
    { "label": "main CI green", "status": "stuck" }
  ],

  "chain": {                                        // blocker chain toward the current milestone
    "nodes": [                                      // ORDER = the main path, drawn left → right
      { "id": "pr140", "label": "Merge PR #140", "sub": "awaiting review", "status": "in_progress", "bottleneck": true },
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
      "waiting_on": "required when status is stuck",
      "evidence": ["#139", "e6cef9e", "`specs/progress-ledger/tasks.md`"],  // or "inferred": true
      "paths": ["src/services/ledger", "src/ui/LedgerPage.tsx"]          // dir/file prefixes or globs (src/**/*.ts); lets the next collect map changed files to this part
    }
  ],

  "next_step": { "title": "Review and merge PR #140", "why": "it unblocks main CI and the round-5 PR" },
  "decisions": [
    { "question": "Ship round 5 as default?", "default": "yes, as in round 5", "deadline": "10-08 10:00", "proposed": true }
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
