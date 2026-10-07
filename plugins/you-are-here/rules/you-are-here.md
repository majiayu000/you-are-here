## Project map (you-are-here)

- Before an unattended run that will take more than ~30 minutes or span several tasks, draw or
  refresh the project map with the you-are-here skill without blocking your own work: start the
  `you-are-here` subagent in the background (Codex: spawn it and don't wait on it). Then turn on
  automatic refreshes with `python3 {{SKILL_DIR}}/scripts/pmap.py auto on <repo>`.
- After a milestone (a tag or release, a merged PR, a finished group of planned tasks), run
  `python3 {{SKILL_DIR}}/scripts/pmap.py status <repo>`. If it prints `stale` or `none`, refresh
  the map the same way.
- When the user asks "where are we?" / "进度到哪了", run the same status command. If `fresh`, answer
  from `<repo>/.project-map/map.json`; otherwise refresh the map first, then answer from it.
- While running unattended, put choices that need the user into the map's `decisions` with a
  default. Only reversible, local choices get a deadline and go ahead on their own; merges, pushes,
  releases, deletions and spending wait for the user's explicit yes.
