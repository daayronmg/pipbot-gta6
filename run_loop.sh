#!/usr/bin/env bash
# Ejecuta el bot cada 30 min durante ~5,5 h dentro de una sola ejecución de GitHub Actions
# (los cron de GitHub se saltan muchas ejecuciones; así no dependemos de ellos).
set -u
END=$(( $(date +%s) + ${LOOP_SECONDS:-19800} ))
git config user.name pipbot
git config user.email pipbot@users.noreply.github.com
while :; do
  git pull -q --rebase || true
  python bot.py 2>&1 | tee run.log
  git add state.json run.log || true
  git add -A publish_now.flag 2>/dev/null || true
  git diff --cached --quiet || (git commit -q -m "estado" && (git push -q || (git pull -q --rebase && git push -q)))
  [ $(( $(date +%s) + 1800 )) -lt $END ] || break
  sleep 1800
done
