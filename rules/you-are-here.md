## Project map (you-are-here skill)

- Before an unattended run that will take more than ~30 minutes or span several tasks, start the
  `you-are-here` subagent **in the background** on the repo and keep working; don't wait for it.
- After each milestone (a release or tag, a merged PR that closes a planned item, or a finished
  group of planned tasks), run `python3 {{SKILL_DIR}}/scripts/pmap.py status <repo>`. If it prints
  `stale` or `none`, start the `you-are-here` subagent in the background again.
- When the user asks "where are we?" / "进度到哪了", run the same status command. If `fresh`,
  answer from `<repo>/.project-map/map.json`. Otherwise update the map first, then answer from it.
- While you run unattended, put choices that need the user into the map's `decisions` with a
  default and a deadline. If there is no answer by the deadline, go ahead with the default, but only
  for reversible, local choices. Anything destructive or outward-facing (push, publish, delete,
  send, spend) still waits for the user's explicit yes.
- If no subagent is available, follow {{SKILL_DIR}}/SKILL.md directly.
