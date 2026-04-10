#!/usr/bin/env bash
# ALIVE x Hermes — Installer
# Detects your situation and sets up the integration.
#
# Usage: bash install.sh
#
# Three paths:
#   A) ALIVE user adding Hermes support
#   B) Hermes user discovering ALIVE
#   C) Power user with existing ad-hoc context system

set -euo pipefail

# Colors
COPPER='\033[38;2;184;115;51m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

echo ""
echo -e "${COPPER}${BOLD}ALIVE × Hermes${RESET}"
echo -e "${DIM}A structured context layer for autonomous agents${RESET}"
echo ""

# ── Detect environment ──────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
WORLD_ROOT=""
HAS_ALIVE=false
HAS_HERMES=false
HAS_ADHOC=false

# Check for Hermes
if command -v hermes &>/dev/null || [ -d "$HERMES_HOME/hermes-agent" ]; then
    HAS_HERMES=true
    echo -e "${GREEN}✓${RESET} Hermes Agent detected at $HERMES_HOME"
else
    echo -e "${YELLOW}○${RESET} Hermes Agent not found"
fi

# Check for ALIVE world
for candidate in \
    "${ALIVE_WORLD_ROOT:-}" \
    "$HOME/world" \
    "$HOME/Library/Mobile Documents/com~apple~CloudDocs/alive"; do
    if [ -n "$candidate" ] && [ -d "$candidate/.alive" ]; then
        WORLD_ROOT="$candidate"
        HAS_ALIVE=true
        break
    fi
done

if $HAS_ALIVE; then
    WALNUT_COUNT=$(find "$WORLD_ROOT" -name "key.md" -path "*/_kernel/*" 2>/dev/null | grep -v "01_Archive" | wc -l | tr -d ' ')
    echo -e "${GREEN}✓${RESET} ALIVE world found at $WORLD_ROOT ($WALNUT_COUNT walnuts)"
else
    echo -e "${YELLOW}○${RESET} ALIVE world not found"
fi

# Check for ad-hoc context systems
if [ -f "$HERMES_HOME/MEMORY.md" ] || [ -f "$HERMES_HOME/USER.md" ]; then
    HAS_ADHOC=true
    echo -e "${YELLOW}○${RESET} Existing Hermes memory files detected (MEMORY.md / USER.md)"
fi

echo ""

# ── Determine path ──────────────────────────────────────────────

if $HAS_ALIVE && $HAS_HERMES; then
    echo -e "${COPPER}Path A:${RESET} ALIVE user adding Hermes support"
    PATH_CHOICE="A"
elif $HAS_HERMES && ! $HAS_ALIVE; then
    echo -e "${COPPER}Path B:${RESET} Hermes user discovering ALIVE"
    PATH_CHOICE="B"
elif $HAS_ADHOC; then
    echo -e "${COPPER}Path C:${RESET} Power user migration"
    PATH_CHOICE="C"
else
    echo -e "${RED}Neither Hermes nor ALIVE detected.${RESET}"
    echo "Install Hermes: https://github.com/NousResearch/hermes-agent"
    echo "Install ALIVE:  claude plugin install alive@alivecontext"
    exit 1
fi

echo ""
echo -e "${DIM}Press Enter to continue, or Ctrl+C to abort.${RESET}"
read -r

# ── Layer 1: Memory Provider ────────────────────────────────────

echo -e "${BOLD}Layer 1: Memory Provider${RESET}"

PROVIDER_DIR="$HERMES_HOME/hermes-agent/plugins/memory/alive"
if [ -d "$PROVIDER_DIR" ]; then
    echo -e "  ${YELLOW}○${RESET} Provider already exists at $PROVIDER_DIR"
    echo -n "  Overwrite? [y/N] "
    read -r OVERWRITE
    if [ "$OVERWRITE" != "y" ] && [ "$OVERWRITE" != "Y" ]; then
        echo "  Skipped."
    else
        cp "$SCRIPT_DIR/memory-provider/__init__.py" "$PROVIDER_DIR/"
        cp "$SCRIPT_DIR/memory-provider/plugin.yaml" "$PROVIDER_DIR/"
        cp "$SCRIPT_DIR/memory-provider/README.md" "$PROVIDER_DIR/"
        echo -e "  ${GREEN}✓${RESET} Memory provider updated"
    fi
else
    mkdir -p "$PROVIDER_DIR"
    cp "$SCRIPT_DIR/memory-provider/__init__.py" "$PROVIDER_DIR/"
    cp "$SCRIPT_DIR/memory-provider/plugin.yaml" "$PROVIDER_DIR/"
    cp "$SCRIPT_DIR/memory-provider/README.md" "$PROVIDER_DIR/"
    echo -e "  ${GREEN}✓${RESET} Memory provider installed"
fi

# Set ALIVE_WORLD_ROOT if we found a world
if $HAS_ALIVE; then
    echo -e "  ${DIM}Set ALIVE_WORLD_ROOT=$WORLD_ROOT in your shell profile${RESET}"
fi

echo ""

# ── Layer 2: Skills ─────────────────────────────────────────────

echo -e "${BOLD}Layer 2: Hermes Skills${RESET}"

SKILLS_DIR="$SCRIPT_DIR/hermes-skills"
CRONS_DIR="$SCRIPT_DIR/cron-templates"

echo "  Skills directory: $SKILLS_DIR"
echo "  Crons directory:  $CRONS_DIR"
echo ""
echo "  Add to ~/.hermes/config.yaml:"
echo ""
echo -e "  ${DIM}skills:"
echo "    external_dirs:"
echo "      - $SKILLS_DIR"
echo -e "      - $CRONS_DIR${RESET}"
echo ""

# Check if config.yaml exists and offer to patch it
HERMES_CONFIG="$HERMES_HOME/config.yaml"
if [ -f "$HERMES_CONFIG" ]; then
    if grep -q "external_dirs" "$HERMES_CONFIG" 2>/dev/null; then
        echo -e "  ${YELLOW}○${RESET} config.yaml already has external_dirs. Add paths manually."
    else
        echo -n "  Add external_dirs to config.yaml? [y/N] "
        read -r ADD_DIRS
        if [ "$ADD_DIRS" = "y" ] || [ "$ADD_DIRS" = "Y" ]; then
            echo "" >> "$HERMES_CONFIG"
            echo "# ALIVE skills and cron templates" >> "$HERMES_CONFIG"
            echo "skills:" >> "$HERMES_CONFIG"
            echo "  external_dirs:" >> "$HERMES_CONFIG"
            echo "    - $SKILLS_DIR" >> "$HERMES_CONFIG"
            echo "    - $CRONS_DIR" >> "$HERMES_CONFIG"
            echo -e "  ${GREEN}✓${RESET} config.yaml updated"
        fi
    fi
else
    echo -e "  ${YELLOW}○${RESET} No config.yaml found. Create one at $HERMES_CONFIG"
fi

echo ""

# ── Layer 3: Crons ──────────────────────────────────────────────

echo -e "${BOLD}Layer 3: Cron Templates${RESET}"
echo -n "  Install 8 ALIVE cron jobs? [y/N] "
read -r INSTALL_CRONS
if [ "$INSTALL_CRONS" = "y" ] || [ "$INSTALL_CRONS" = "Y" ]; then
    echo -n "  Deliver notifications to? [telegram/local] "
    read -r DELIVER
    DELIVER="${DELIVER:-telegram}"
    bash "$SCRIPT_DIR/setup-crons.sh" "$DELIVER"
else
    echo "  Skipped. Run 'bash $SCRIPT_DIR/setup-crons.sh' later."
fi

echo ""

# ── Layer 4: Runtime Integration ────────────────────────────────

echo -e "${BOLD}Layer 4: Runtime Integration${RESET}"

# SOUL.md patch
SOUL_FILE="$HERMES_HOME/SOUL.md"
if [ -f "$SOUL_FILE" ]; then
    if grep -q "squirrel" "$SOUL_FILE" 2>/dev/null; then
        echo -e "  ${YELLOW}○${RESET} SOUL.md already has squirrel patch"
    else
        echo -n "  Append squirrel patch to SOUL.md? [y/N] "
        read -r PATCH_SOUL
        if [ "$PATCH_SOUL" = "y" ] || [ "$PATCH_SOUL" = "Y" ]; then
            echo "" >> "$SOUL_FILE"
            echo "You share this world with a squirrel -- the ALIVE context runtime." >> "$SOUL_FILE"
            echo "It scatterhoards context across walnuts so you can focus on helping" >> "$SOUL_FILE"
            echo "the user. Read what it leaves. Write what it needs. Don't fight it." >> "$SOUL_FILE"
            echo -e "  ${GREEN}✓${RESET} SOUL.md patched"
        fi
    fi
else
    echo -e "  ${DIM}No SOUL.md found. The patch will be applied when you create one.${RESET}"
fi

# AGENTS.md
if $HAS_ALIVE; then
    AGENTS_DEST="$WORLD_ROOT/AGENTS.md"
    if [ -f "$AGENTS_DEST" ]; then
        echo -e "  ${YELLOW}○${RESET} AGENTS.md already exists at $AGENTS_DEST"
    else
        cp "$SCRIPT_DIR/agents.md" "$AGENTS_DEST"
        echo -e "  ${GREEN}✓${RESET} AGENTS.md installed at $AGENTS_DEST"
    fi
fi

echo ""

# ── Path B: Scaffold world ─────────────────────────────────────

if [ "$PATH_CHOICE" = "B" ]; then
    echo -e "${BOLD}Scaffolding ALIVE World${RESET}"
    echo ""
    echo "  ALIVE needs a world directory. Recommended: ~/world"
    echo -n "  Create ~/world? [y/N] "
    read -r CREATE_WORLD
    if [ "$CREATE_WORLD" = "y" ] || [ "$CREATE_WORLD" = "Y" ]; then
        WORLD_ROOT="$HOME/world"
        mkdir -p "$WORLD_ROOT/.alive/_squirrels"
        mkdir -p "$WORLD_ROOT/02_Life/people"
        mkdir -p "$WORLD_ROOT/03_Inbox"
        mkdir -p "$WORLD_ROOT/04_Ventures"
        mkdir -p "$WORLD_ROOT/05_Experiments"
        mkdir -p "$WORLD_ROOT/01_Archive"

        # World key
        CREATED_DATE=$(date +%Y-%m-%d)
        cat > "$WORLD_ROOT/.alive/key.md" << WORLDKEY
---
name: (your name)
created: $CREATED_DATE
---

Your ALIVE world. Personal context infrastructure.
WORLDKEY

        # Preferences
        cat > "$WORLD_ROOT/.alive/preferences.yaml" << 'PREFS'
spark: true
health_nudges: true
PREFS

        echo -e "  ${GREEN}✓${RESET} World created at $WORLD_ROOT"
        echo ""
        echo "  Next: install the full ALIVE plugin for Claude Code:"
        echo "    claude plugin install alive@alivecontext"
        echo ""
        echo "  Or just use Hermes with the skills and memory provider."
    fi
fi

# ── Done ────────────────────────────────────────────────────────

echo ""
echo -e "${COPPER}${BOLD}Setup complete.${RESET}"
echo ""
echo "  Memory provider: hermes memory setup -> select 'alive'"
echo "  Skills:          /alive-load, /alive-save, /alive-world, ..."
echo "  Crons:           hermes cron list"
echo ""
echo -e "  ${DIM}The AI is the engine. The context is the fuel. And the fuel compounds.${RESET}"
echo ""
