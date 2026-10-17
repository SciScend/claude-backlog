# claude-backlog

- Use only ASCII punctuation (ASCII range 32-126) in code, docs and commit
  messages: no typographic dashes, quotes or arrows.
- Keep the tool split: claude-nightshift runs one task across usage windows;
  claude-backlog owns the queue, the board UI, starting and stopping runs
  (Run now, away detection), git worktrees, `systemd-inhibit`, task
  conventions and the reports. Do not reimplement
  usage or budget logic here; call `claude-nightshift` and act on its exit
  code. If a change needs nightshift to behave differently, change
  nightshift.
- This project will be published on GitHub for others to use: no personal
  paths, names or machine-specific settings in code, defaults, tests or
  examples; English for UI, docs and code.
- Status: phase 1 (docs/design.md, section 11). The design is reviewed;
  change it first when the implementation needs to differ from it.
