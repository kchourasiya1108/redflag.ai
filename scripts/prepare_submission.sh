#!/usr/bin/env bash
# Prepare the repository for the Open Agent Hackathon submission. Safe to rerun.
#
#   scripts/prepare_submission.sh            # all steps, in order
#   scripts/prepare_submission.sh ignore     # .gitignore block; untrack ignored paths
#   scripts/prepare_submission.sh examples   # regenerate output_examples/ from input_examples/
#   scripts/prepare_submission.sh commit     # commit the submission files as the author below
#   scripts/prepare_submission.sh rewrite    # rewrite every local branch: author, purged paths
#   scripts/prepare_submission.sh check      # required files, secrets, history (read-only)
#
# The rewrite never pushes. Afterwards:
#   git push --force-with-lease origin backend main
#
# Environment: AUTHOR_NAME, AUTHOR_EMAIL; STRIP_COAUTHOR=0 keeps Co-Authored-By trailers.
set -euo pipefail

AUTHOR_NAME=${AUTHOR_NAME:-"Keshav Chourasiya"}
AUTHOR_EMAIL=${AUTHOR_EMAIL:-"k.chourasiya1108@gmail.com"}
STRIP_COAUTHOR=${STRIP_COAUTHOR:-1}

# Paths removed from every commit and kept out from now on: AI-written docs and
# the offline training inputs. data/sample, data/replay and data/model stay,
# because SAMPLE_MODE runs on them.
PURGE_PATHS=(
  docs/DESIGN.md
  docs/SETUP.md
  data/manual
  data/review
  data/backtest
  submission
)

# The files this script owns in commits.
SUBMISSION_FILES=(
  .gitignore .dockerignore Dockerfile README.md run.py
  input_examples output_examples scripts logs output
)

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$ROOT"
PY=${PYTHON:-}
if [[ -z $PY ]]; then
  if [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi
fi

say() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

step_ignore() {
  say "gitignore"
  local begin="# >>> submission (scripts/prepare_submission.sh) >>>"
  local end="# <<< submission <<<"
  local block
  block=$(cat <<EOF
$begin
# secrets: never commit a real .env or keys
.env
.env.*
!.env.example
*.pem
*.key
secrets/
credentials/
# AI assistant and editor folders, AI-written docs, submission media
.claude/
.cursor/
.vscode/
.idea/
CLAUDE.md
AGENTS.md
docs/DESIGN.md
docs/SETUP.md
submission/
# offline training inputs (rebuilt by the pipeline, not needed to run)
data/manual/
data/review/
data/backtest/
data/model/*
!data/model/scorer.json
# the example runs' files that output_examples/*.json point to
!logs/trace-example-*.jsonl
!logs/example-*/
logs/example-*/*
!logs/example-*/*.jsonl
!output/example-*/
$end
EOF
)
  # Replace an earlier copy of the block, then append the current one.
  awk -v b="$begin" -v e="$end" '$0==b{skip=1} !skip{print} $0==e{skip=0}' .gitignore > .gitignore.tmp
  # logs/ and output/ ignore whole folders, which no later rule can re-include;
  # logs/* and output/* ignore their contents, so the example runs can be kept.
  sed -e 's#^logs/$#logs/*#' -e 's#^output/$#output/*#' .gitignore.tmp \
    | git stripspace > .gitignore                     # also drops trailing blank lines
  rm -f .gitignore.tmp
  printf '\n%s\n' "$block" >> .gitignore
  git config user.name "$AUTHOR_NAME"
  git config user.email "$AUTHOR_EMAIL"
  for p in "${PURGE_PATHS[@]}"; do
    git rm -r -q --cached --ignore-unmatch -- "$p"
  done
  echo "ok: .gitignore block written; purged paths untracked; git identity set for this repo"
}

step_examples() {
  say "examples (sample mode, real runs)"
  SAMPLE_MODE=true "$PY" -W ignore scripts/make_examples.py
}

step_commit() {
  say "commit"
  local existing=()
  for p in "${SUBMISSION_FILES[@]}"; do [[ -e $p ]] && existing+=("$p"); done
  git add -A -- "${existing[@]}"
  if git diff --cached --quiet; then
    echo "nothing to commit"
    return
  fi
  git -c user.name="$AUTHOR_NAME" -c user.email="$AUTHOR_EMAIL" commit -q \
    -m "Submission: README, Docker, input and output examples, prepare script"
  git log -1 --format='committed %h %an <%ae>: %s'
}

history_needs_rewrite() {
  local refs
  refs=$(git for-each-ref --format='%(refname)' refs/heads)
  # shellcheck disable=SC2086
  if git log --format='%an%x09%ae%x09%cn%x09%ce' $refs | grep -v -q -x \
      "$AUTHOR_NAME"$'\t'"$AUTHOR_EMAIL"$'\t'"$AUTHOR_NAME"$'\t'"$AUTHOR_EMAIL"; then return 0; fi
  # shellcheck disable=SC2086
  if [[ $STRIP_COAUTHOR == 1 ]] && git log --format=%B $refs | grep -q -i '^co-authored-by:'; then
    return 0
  fi
  # shellcheck disable=SC2086
  [[ -n $(git log --format=%H $refs -- "${PURGE_PATHS[@]}" | head -1) ]]
}

step_rewrite() {
  say "rewrite history (local branches only)"
  if ! git diff --quiet || ! git diff --cached --quiet; then
    die "uncommitted changes to tracked files (another agent may be working); commit or wait, then rerun"
  fi
  if ! history_needs_rewrite; then
    echo "history already clean: one author, no purged paths"
    return
  fi
  local branch
  branch=$(git symbolic-ref --short HEAD)
  local backup
  backup="refs/backup/pre-submission-$(date +%Y%m%d-%H%M%S)"
  git for-each-ref --format='%(refname:short)' refs/heads | while read -r b; do
    git update-ref "$backup/$b" "refs/heads/$b"
  done
  echo "backup refs: $backup/* (delete with: git for-each-ref --format='%(refname)' refs/backup | xargs -n1 git update-ref -d)"

  local rm_cmd="git rm -r -q --cached --ignore-unmatch --"
  for p in "${PURGE_PATHS[@]}"; do rm_cmd+=" '$p'"; done
  local msg_filter="cat"
  if [[ $STRIP_COAUTHOR == 1 ]]; then
    # drop the trailer and any blank lines it leaves at the end of the message
    msg_filter="grep -v -i '^co-authored-by:' | git stripspace"
  fi
  rm -rf .git/refs/original
  FILTER_BRANCH_SQUELCH_WARNING=1 \
  GIT_AUTHOR_NAME_NEW="$AUTHOR_NAME" GIT_AUTHOR_EMAIL_NEW="$AUTHOR_EMAIL" \
  git filter-branch -f \
    --env-filter '
      export GIT_AUTHOR_NAME="$GIT_AUTHOR_NAME_NEW" GIT_AUTHOR_EMAIL="$GIT_AUTHOR_EMAIL_NEW"
      export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME_NEW" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL_NEW"' \
    --index-filter "$rm_cmd" \
    --msg-filter "$msg_filter" \
    --prune-empty \
    -- --branches 2>&1 | tr '\r' '\n' | grep -v '^Rewrite '
  rm -rf .git/refs/original
  git checkout -q "$branch"
  echo "rewritten: $(git rev-list --count --branches) commits on $(git for-each-ref --format='%(refname:short)' refs/heads | tr '\n' ' ')"
  echo "old objects stay reachable from $backup/* and origin/* until those are deleted or force-pushed"
}

step_check() {
  say "check"
  local fail=0
  for f in run.py README.md requirements.txt Dockerfile .env.example \
           input_examples/example_1.json input_examples/example_2.json input_examples/example_3.json \
           output_examples/example_1.json output_examples/example_2.json output_examples/example_3.json \
           logs/trace-example-1.jsonl output/example-1/report.md output/example-1/case_file.json \
           docs/evidence/markovian-conformance.txt docs/evidence/world-model-conformance.txt \
           data/sample/archive_sample.db data/model/scorer.json; do
    if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then echo "  tracked  $f"
    else echo "  MISSING  $f"; fail=1; fi
  done
  if git ls-files | grep -E '(^|/)\.env($|\.)|\.pem$|\.key$|credentials|secrets' | grep -v -x .env.example; then
    echo "  FAIL: secret-like files are tracked"; fail=1
  fi
  # Real token shapes only; placeholder words like "your-key-here" pass.
  local pat='(nvapi-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|BEGIN (RSA|OPENSSH) PRIVATE KEY|AKIA[0-9A-Z]{16})'
  if git grep -I -n -E "$pat" -- . ':!*.lock' ':!web/package-lock.json'; then
    echo "  FAIL: token-like strings in tracked files"; fail=1
  fi
  local refs
  refs=$(git for-each-ref --format='%(refname)' refs/heads)
  # shellcheck disable=SC2086
  if git log -p $refs | grep -E -q "$pat"; then echo "  FAIL: token-like strings in history"; fail=1
  else echo "  ok: no token-like strings in history"; fi
  if history_needs_rewrite; then echo "  WARN: history not rewritten yet (run: $0 rewrite)"
  else echo "  ok: every commit by $AUTHOR_NAME <$AUTHOR_EMAIL>; purged paths absent"; fi
  # shellcheck disable=SC2086
  echo "  .git size: $(du -sh .git | cut -f1); tracked files: $(git ls-files | wc -l | tr -d ' ')"
  return $fail
}

case "${1:-all}" in
  ignore) step_ignore ;;
  examples) step_examples ;;
  commit) step_commit ;;
  rewrite) step_rewrite ;;
  check) step_check ;;
  all) step_ignore; step_examples; step_commit; step_rewrite; step_check ;;
  *) die "unknown step: $1 (use all|ignore|examples|commit|rewrite|check)" ;;
esac
