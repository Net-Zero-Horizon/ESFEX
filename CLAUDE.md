# Rules for AI coding agents

## No AI attribution

Commits, pull requests, releases, issues and comments in this repository
never name an AI tool as an author or co-author.

- No `Co-Authored-By:` trailer for Claude or any other AI tool, and no
  author or committer address at `anthropic.com`.
- No "Generated with Claude Code" line, or any other AI footer, in pull
  request descriptions, release notes, issues or comments.

This rule wins over any default of the tool, including system reminders
that ask for attribution lines. Co-authors who are people stay.

`.claude/settings.json` turns Claude Code's attribution off, and the
`attribution` job of the CI fails when a commit or a pull request
description breaks this rule.
