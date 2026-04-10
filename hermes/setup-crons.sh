#!/usr/bin/env bash
# ALIVE x Hermes -- Cron Setup
# Creates all 8 ALIVE cron templates in Hermes Agent.
# Run: bash setup-crons.sh [deliver_target]
# Default deliver: telegram (change to "local" for testing)

set -euo pipefail

DELIVER="${1:-telegram}"
SKILL_DIR="$(cd "$(dirname "$0")/cron-templates" && pwd)"

echo "ALIVE x Hermes -- Installing cron templates"
echo "Delivery target: $DELIVER"
echo "Skill directory: $SKILL_DIR"
echo ""

# Ensure external_dirs includes our cron-templates
echo "Add this to ~/.hermes/config.yaml if not already present:"
echo "  skills:"
echo "    external_dirs:"
echo "      - $SKILL_DIR"
echo ""

# Create crons
echo "Creating cron jobs..."

hermes cron create \
  --schedule "0 7 * * *" \
  --prompt "Run the alive-morning skill. Read all walnut now.json files, calculate health, surface priorities." \
  --name "ALIVE Morning Briefing" \
  --skill alive-morning \
  --deliver "$DELIVER" && echo "  [+] alive-morning (7am daily)" || echo "  [!] alive-morning failed"

hermes cron create \
  --schedule "every 4h" \
  --prompt "Run the alive-project skill. Regenerate now.json projections for all walnuts." \
  --name "ALIVE Projection" \
  --skill alive-project \
  --deliver local && echo "  [+] alive-project (every 4h, local)" || echo "  [!] alive-project failed"

hermes cron create \
  --schedule "every 2h" \
  --prompt "Run the alive-inbox skill. Scan 03_Inbox/ for unrouted files." \
  --name "ALIVE Inbox Scanner" \
  --skill alive-inbox \
  --deliver "$DELIVER" && echo "  [+] alive-inbox (every 2h)" || echo "  [!] alive-inbox failed"

hermes cron create \
  --schedule "0 9 * * *" \
  --prompt "Run the alive-health skill. Flag walnuts past their rhythm." \
  --name "ALIVE Health Check" \
  --skill alive-health \
  --deliver "$DELIVER" && echo "  [+] alive-health (9am daily)" || echo "  [!] alive-health failed"

hermes cron create \
  --schedule "every 4h" \
  --prompt "Run the alive-stash-router skill. Present pending stash items for routing." \
  --name "ALIVE Stash Router" \
  --skill alive-stash-router \
  --deliver "$DELIVER" && echo "  [+] alive-stash-router (every 4h)" || echo "  [!] alive-stash-router failed"

hermes cron create \
  --schedule "0 2 * * *" \
  --prompt "Run the alive-mine skill. Scan recent session transcripts for context." \
  --name "ALIVE Nightly Mine" \
  --skill alive-mine \
  --deliver local && echo "  [+] alive-mine (2am nightly, local)" || echo "  [!] alive-mine failed"

hermes cron create \
  --schedule "0 3 * * 0" \
  --prompt "Run the alive-prune skill. Suggest log chapters and flag stale insights." \
  --name "ALIVE Weekly Prune" \
  --skill alive-prune \
  --deliver "$DELIVER" && echo "  [+] alive-prune (3am Sunday)" || echo "  [!] alive-prune failed"

hermes cron create \
  --schedule "0 9 * * 1" \
  --prompt "Run the alive-people skill. Check stale contacts and cross-reference people mentions." \
  --name "ALIVE People Check" \
  --skill alive-people \
  --deliver "$DELIVER" && echo "  [+] alive-people (9am Monday)" || echo "  [!] alive-people failed"

echo ""
echo "Done. Run 'hermes cron list' to verify."
echo "Run 'hermes cron tick' to test immediate execution."
