#!/usr/bin/env bash
# Installs the Claude Code plugins and developer tools used on this project.
#   Plugins (project scope, from anthropics/claude-plugins-official):
#     supabase, playwright, context7, frontend-design
#   CLIs: Playwright CLI (@playwright/cli) + its Claude skill, Strix (strix-agent)
set -euo pipefail
cd "$(dirname "$0")/.."

if command -v claude >/dev/null 2>&1; then
  claude plugin marketplace add anthropics/claude-plugins-official --scope project || true
  for plugin in supabase playwright context7 frontend-design; do
    claude plugin install "${plugin}@claude-plugins-official" --scope project -y
  done
else
  echo "Claude Code CLI not found; plugins are still declared in .claude/settings.json" >&2
fi

# Playwright CLI and its Claude Code skill (.claude/skills/playwright-cli)
npm install -g @playwright/cli@latest
playwright-cli install --skills

# Strix (requires Python 3.12+ and a running Docker daemon to scan)
if command -v uv >/dev/null 2>&1; then
  uv tool install strix-agent --python 3.12
elif command -v pipx >/dev/null 2>&1; then
  pipx install strix-agent
else
  echo "Install uv or pipx, then run: uv tool install strix-agent --python 3.12" >&2
fi

echo "Done. See .claude/skills/strix-scan/SKILL.md to run a security scan."
