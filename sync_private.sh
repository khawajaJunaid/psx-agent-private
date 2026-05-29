#!/usr/bin/env bash
# sync_private.sh — wire up the two-remote setup and push everything to private.
# Run once from your local machine:
#   cd /Users/mac/Documents/personal_projects/psx-agent
#   bash sync_private.sh

set -e

PRIVATE_REMOTE="git@github.com:khawajaJunaid/psx-agent-private.git"
BRANCH="claude/peaceful-bohr-Kf1Wy"

echo "==> Checking remotes..."
if git remote get-url origin 2>/dev/null | grep -q "psx-agent-private"; then
  echo "    origin already points to private repo — skipping rename"
elif git remote get-url public 2>/dev/null | grep -q "psx-agent"; then
  echo "    remotes already split — skipping rename"
else
  echo "    renaming origin -> public"
  git remote rename origin public
  echo "    adding private repo as origin"
  git remote add origin "$PRIVATE_REMOTE"
fi

echo "==> Fetching branch from public remote..."
git fetch public "$BRANCH"

echo "==> Checking out $BRANCH..."
git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "public/$BRANCH"

echo "==> Pulling latest from public..."
git pull public "$BRANCH" --no-rebase

echo "==> Swapping to private gitignore..."
if [ ! -f .gitignore-public ]; then
  cp .gitignore .gitignore-public
fi
cp .gitignore-private .gitignore

echo "==> Staging sensitive data files..."
git add -f db.sqlite profile.yaml .gitignore 2>/dev/null || true

if git diff --cached --quiet; then
  echo "    nothing new to commit — data files already up to date"
else
  git commit -m "sync local db.sqlite and profile.yaml to private ($(date '+%Y-%m-%d %H:%M'))"
fi

echo "==> Pushing to private repo..."
git push -u origin "$BRANCH"

echo ""
echo "Done. Your two-remote setup:"
git remote -v
echo ""
echo "Going forward:"
echo "  git push origin   # private — saves db.sqlite, profile.yaml, everything"
echo "  git push public   # public  — code only, sensitive files stay gitignored"
