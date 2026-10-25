#!/usr/bin/env bash
# install-opencode.sh — install the opencode_skill skills into OpenCode.
#
# OpenCode discovers skills from ~/.config/opencode/skills/<name>/SKILL.md
# (global) or .opencode/skills/<name>/SKILL.md (per-project). OpenCode has no
# {baseDir} placeholder, so we substitute it with the real install path in the
# copied SKILL.md, leaving the source tree untouched.
#
# Every install snapshots the live directory first, so a bad version can be
# undone with --rollback without needing the git history.
#
# Usage:
#   ./install-opencode.sh                 # install all skills globally
#   ./install-opencode.sh --project DIR   # install into DIR/.opencode/skills AND DIR/AGENTS.md (global rules)
#   ./install-opencode.sh --dest D --agents-md P   # custom skills dir + where AGENTS.md goes (omit = loud warning)
#   ./install-opencode.sh sqltune slowsql # install only the named skills
#   ./install-opencode.sh --dry-run       # show what would happen
#   ./install-opencode.sh --versions      # show what is installed + snapshots
#   ./install-opencode.sh --rollback      # restore the newest snapshot
#   ./install-opencode.sh --rollback DIR  # restore a specific snapshot
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${HOME}/.config/opencode/skills"
DRY=0
ONLY=()
DO_ROLLBACK=0
ROLLBACK_FROM=""
SHOW_VERSIONS=0
NO_BACKUP=0
KEEP=5
PROJECT_ROOT=""
AGENTS_DEST=""

while [ $# -gt 0 ]; do
  case "$1" in
    --project) shift; PROJECT_ROOT="${1:?--project needs a dir}"; DEST="$PROJECT_ROOT/.opencode/skills" ;;
    --dest)    shift; DEST="${1:?--dest needs a dir}" ;;
    --agents-md) shift; AGENTS_DEST="${1:?--agents-md needs a path}" ;;
    --dry-run) DRY=1 ;;
    --versions|--list-versions) SHOW_VERSIONS=1 ;;
    --no-backup) NO_BACKUP=1 ;;
    --keep)    shift; KEEP="${1:?--keep needs a number}" ;;
    --rollback)
      DO_ROLLBACK=1
      # Optional argument: a snapshot dir. Anything starting with - is the
      # next option, not our argument.
      case "${2:-}" in ""|-*) ;; *) ROLLBACK_FROM="$2"; shift ;; esac
      ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    --*) echo "unknown option: $1" >&2; exit 2 ;;
    *) ONLY+=("$1") ;;
  esac
  shift
done

# --project: AGENTS.md goes to the project root (opencode reads global rules there);
# an explicit --agents-md wins.
[ -n "$PROJECT_ROOT" ] && [ -z "$AGENTS_DEST" ] && AGENTS_DEST="$PROJECT_ROOT/AGENTS.md"

run() { if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }
want() { [ "${#ONLY[@]}" -eq 0 ] && return 0; for x in "${ONLY[@]}"; do [ "$x" = "$1" ] && return 0; done; return 1; }

STAMP="${DEST}/.installed-version"

# Snapshot dirs, oldest first. **Must print nothing and still exit 0** when
# there are none: `snaps="$(snapshots)"` is a bare assignment, so a non-zero
# status there kills the whole script under `set -e`. On a fresh machine the
# parent dir doesn't exist yet and `find` fails — which made `--versions`
# exit 1 while printing a perfectly normal-looking report.
snapshots() {
  local parent
  parent="$(dirname "$DEST")"
  [ -d "$parent" ] || return 0
  find "$parent" -maxdepth 1 -type d \
       -name "$(basename "$DEST").bak.*" 2>/dev/null | sort || true
}

# A free snapshot path. The timestamp is second-resolution, so two installs
# within the same second would otherwise `cp -R` the tree *into* the existing
# snapshot (DEST.bak.TS/skills/...) — a corrupt snapshot that reports success
# and only reveals itself when someone tries to roll back to it.
new_snapshot_path() {
  local base="${DEST}.bak.$(date +%Y%m%d-%H%M%S)" n=2
  local p="$base"
  while [ -e "$p" ]; do p="${base}-${n}"; n=$((n + 1)); done
  echo "$p"
}

# --- --versions --------------------------------------------------------------
if [ "$SHOW_VERSIONS" = 1 ]; then
  echo "• install dir: $DEST"
  if [ -f "$STAMP" ]; then
    sed 's/^/  /' "$STAMP"
  elif [ -d "$DEST" ]; then
    echo "  (installed before version stamping — no provenance recorded)"
  else
    echo "  (nothing installed)"
  fi
  echo
  echo "• snapshots (oldest first; --rollback restores the newest):"
  snaps="$(snapshots)"
  if [ -z "$snaps" ]; then
    echo "  (none)"
  else
    echo "$snaps" | while read -r s; do
      [ -n "$s" ] || continue
      if [ -f "$s/.installed-version" ]; then
        desc="$(grep -E '^(commit|installed):' "$s/.installed-version" | tr '\n' ' ')"
      else
        desc="(no stamp)"
      fi
      printf "  %s  %s\n" "$s" "$desc"
    done
  fi
  exit 0
fi

# --- --rollback --------------------------------------------------------------
if [ "$DO_ROLLBACK" = 1 ]; then
  if [ -n "$ROLLBACK_FROM" ]; then
    from="$ROLLBACK_FROM"
  else
    from="$(snapshots | tail -1)"
  fi
  # Fail closed: a rollback that silently does nothing is worse than an error,
  # because the operator walks away believing the bad version is gone.
  [ -n "$from" ] || { echo "✗ no snapshot to roll back to" >&2; exit 1; }
  [ -d "$from" ] || { echo "✗ not a directory: $from" >&2; exit 1; }
  ls "$from"/*/SKILL.md >/dev/null 2>&1 \
    || { echo "✗ $from holds no SKILL.md — refusing to restore it" >&2; exit 1; }

  echo "• rolling back $DEST"
  echo "  from snapshot: $from"
  if [ -d "$DEST" ]; then
    # Keep the version being replaced as a snapshot too, so the rollback is
    # itself reversible.
    keep="$(new_snapshot_path)"
    echo "  current version saved as: $keep"
    run "mv \"$DEST\" \"$keep\""
  fi
  run "cp -R \"$from\" \"$DEST\""
  echo "✓ rolled back ($(ls -1 "$DEST" 2>/dev/null | wc -l | tr -d ' ') entries)"
  [ "$DRY" = 1 ] && echo "(dry-run: nothing was written)"
  exit 0
fi

# --- prerequisite check: python3 + required modules --------------------------
echo "• checking prerequisites"
command -v python3 >/dev/null || { echo "✗ python3 not found" >&2; exit 1; }
missing=""
for m in psycopg2 cryptography yaml; do
  python3 -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$m') else 1)" \
    || missing="$missing $m"
done
if [ -n "$missing" ]; then
  echo "! missing Python modules:$missing"
  echo "  install with: python3 -m pip install -r \"$SRC/requirements.txt\""
fi

# --- snapshot the live install before touching it ----------------------------
# The install below is `rm -rf` + `cp`. Without this, a version that misbehaves
# in OpenCode can only be undone by checking out an older commit and
# reinstalling — and that assumes the operator knows which commit was live.
if [ -d "$DEST" ] && [ "$NO_BACKUP" = 0 ]; then
  snap="$(new_snapshot_path)"
  echo "• snapshotting current install -> $snap"
  run "cp -R \"$DEST\" \"$snap\""

  # Retention. Deleting is the one irreversible step here, so it only ever
  # touches dirs matching our own snapshot pattern.
  if [ "$DRY" = 0 ] && [ "$KEEP" -gt 0 ]; then
    total="$(snapshots | grep -c . || true)"
    if [ "$total" -gt "$KEEP" ]; then
      snapshots | head -n "$((total - KEEP))" | while read -r old; do
        [ -n "$old" ] || continue
        echo "  pruning old snapshot: $old"
        rm -rf "$old"
      done
    fi
  fi
elif [ "$NO_BACKUP" = 1 ]; then
  echo "• --no-backup: skipping snapshot (rollback will not have this version)"
fi

# --- install each skill ------------------------------------------------------
echo "• installing skills into $DEST"
run "mkdir -p \"$DEST\""

# The shared connection layer travels with the skills. It has no SKILL.md, so
# OpenCode ignores it as a skill; the scripts locate it by walking up to here.
echo "  → common/ (shared connection layer)"
run "rm -rf \"$DEST/common\""
run "cp -R \"$SRC/common\" \"$DEST/common\""

# The whitelist scripts the skills actually execute. **This was missing** —
# the installer shipped skill code but not scripts/registry/, so the two drifted:
# an install had 89 scripts while the repo had 90, and sqltune died with
# KeyError 'curpages' because the deployed tables.yaml predated that column.
# That failure was at least loud; a changed SQL *body* would have been silent —
# the skill would keep running the old query and nobody would know.
echo "  → scripts/registry/ (whitelist scripts the skills execute)"
run "mkdir -p \"$DEST/scripts\""
run "rm -rf \"$DEST/scripts/registry\""
run "cp -R \"$SRC/scripts/registry\" \"$DEST/scripts/registry\""

count=0
installed=""
for d in "$SRC"/skills/*/; do
  [ -f "${d}SKILL.md" ] || continue
  name="$(basename "$d")"
  want "$name" || continue
  target="$DEST/$name"
  ver="$(grep -E '^version:' "${d}SKILL.md" | head -1 | sed -E 's/^version:[[:space:]]*//')"
  echo "  → $name (v${ver:-?})"
  installed="${installed}${name} v${ver:-?}"$'\n'
  run "rm -rf \"$target\""
  run "cp -R \"$d\" \"$target\""
  # Substitute {baseDir} (this skill's install dir) and {kbDir} (the user
  # knowledge base, a sibling of skills/ so a reinstall's rm -rf cannot eat it).
  if [ "$DRY" = 0 ]; then
    python3 - "$target" <<'PY'
import pathlib, sys
base = pathlib.Path(sys.argv[1])          # <root>/skills/<name>
kbdir = base.parent.parent / "kb"         # <root>/kb  —— 与 skills/ 同级
skill = base / "SKILL.md"
skill.write_text(
    skill.read_text().replace("{baseDir}", str(base)).replace("{kbDir}", str(kbdir))
)
PY
  else
    echo "  [dry-run] substitute {baseDir} -> $target, {kbDir} -> <root>/kb in SKILL.md"
  fi
  count=$((count + 1))
done

# --- AGENTS.md: global rules (security red lines, sql_id / schema rules) ------
# opencode reads it from the **project root**, not from skills/. Shipping the
# skills without it is exactly how the 2026-09-10 "the red lines were deleted"
# report happened, so either install it here (--project / --agents-md) or say
# loudly that we did not.
if [ -n "$AGENTS_DEST" ]; then
  if [ -f "$AGENTS_DEST" ] && ! cmp -s "$SRC/AGENTS.md" "$AGENTS_DEST"; then
    abak="${AGENTS_DEST}.bak.$(date +%Y%m%d-%H%M%S)"
    echo "• backing up existing $AGENTS_DEST -> $abak"
    run "cp -p \"$AGENTS_DEST\" \"$abak\""
  fi
  echo "• installing AGENTS.md -> $AGENTS_DEST"
  run "cp \"$SRC/AGENTS.md\" \"$AGENTS_DEST\""
else
  echo "! AGENTS.md NOT installed (global rules incl. the security red lines)."
  echo "  use --project <opencode project root> or --agents-md <path>, or copy $SRC/AGENTS.md to the project root yourself"
fi

# --- record provenance -------------------------------------------------------
# So `--versions` can answer "what is live right now, and from which commit".
# Without it, a snapshot is just an unlabelled directory.
if [ "$DRY" = 0 ]; then
  {
    echo "installed: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "source:    $SRC"
    if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
      echo "commit:    $(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo '?')"
      echo "tag:       $(git -C "$SRC" describe --tags --always 2>/dev/null || echo '?')"
      if [ -n "$(git -C "$SRC" status --porcelain 2>/dev/null)" ]; then
        echo "dirty:     yes (source tree had uncommitted changes)"
      fi
    fi
    if [ "${#ONLY[@]}" -gt 0 ]; then
      echo "partial:   yes (only ${ONLY[*]})"
    fi
    echo "skills:"
    printf '%s' "$installed" | sed 's/^/  /'
  } > "$STAMP"
fi

echo "✓ installed $count skill(s)"
[ "$DRY" = 1 ] && echo "(dry-run: nothing was written)"
echo
echo "Next:"
echo "  1) ensure deps:  python3 -m pip install -r \"$SRC/requirements.txt\""
echo "  2) ensure a DB connection exists in ~/.gdaa (see docs/INSTALL-opencode.md)"
echo "  3) in opencode, the skills appear via the native 'skill' tool"
echo "  4) AGENTS.md must sit in the opencode project root (installed above only with --project / --agents-md)"
echo
echo "If this version misbehaves:  $0 --rollback"
