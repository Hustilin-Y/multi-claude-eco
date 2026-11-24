#!/usr/bin/env bash
# Runs one user's assistant inside the "u-<name>" tmux session (socket -L
# assistant), started by `assistant start-users`, and by the router with "fresh" for
# /restart. Keeps a Claude Code session open in that user's workspace, restarting it
# if it exits. The router types the user's messages into it, and it replies with `tg`
# (or an add-in's command, for an app the add-in brings).
#
# A person with an "isolated" entry in users.json runs as that entry's Linux user: this script only runs
# its launcher through sudo (`sudo -n -u LINUX_USER LAUNCHER [--continue]`), a root-owned program that
# sudoers lets this user run as them and nothing else. The launcher sets up their environment and
# workspace and starts claude with its own fixed flags, so nothing typed here can loosen its permission
# mode or settings. No bot env and no `tg` reach it.
set -u

name=${1:?usage: run-user.sh NAME [fresh]}
# shellcheck source-path=SCRIPTDIR source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
WS=$(user_workspace "$name")
SESSIONS=$(user_project_dir "$name")
QUICK_EXIT=30  # a session that dies sooner than this after starting isn't resumed next time
lu=$(user_isolated "$name" linux_user)

if [ -n "$lu" ]; then
  launcher=$(user_isolated "$name" launcher)
  getent passwd "$lu" >/dev/null || { echo "no Linux user $lu"; sleep 60; exit 1; }
  case "$launcher" in
    /*) ;;
    *) echo "no launcher for $name: isolated.launcher must be an absolute path"; sleep 60; exit 1 ;;
  esac
else
  cd "$WS" || { echo "no workspace at $WS"; sleep 60; exit 1; }
  load_env
  TG_CHAT_ID=$(user_field "$name" telegram_id)
  export TG_CHAT_ID ASSISTANT_USER="$name" CLAUDE_CODE_SANDBOXED=1
  if addon_on locations; then
    # Tools and plugins find the latest location the person shared on Telegram here.
    export ASSISTANT_LOCATION="$ROOT/state/locations/$TG_CHAT_ID.json"
  fi
fi

resume=1
[ "${2:-}" = fresh ] && resume=0  # /restart: begin with a new conversation
while true; do
  resumed=()
  # Pick up the previous conversation so context survives restarts. If that
  # session dies right away (e.g. it's corrupt), start fresh next time.
  if [ "$resume" = 1 ] && compgen -G "$SESSIONS/*.jsonl" >/dev/null; then
    resumed=(--continue)
  fi
  started=$(date +%s)
  if [ -n "$lu" ]; then
    sudo -n -u "$lu" "$launcher" "${resumed[@]}"
  else
    claude --permission-mode bypassPermissions --name "$name" --settings "$TELEGRAM_PLUGIN_OFF" "${resumed[@]}"
  fi
  code=$?
  [ "${RUN_USER_ONCE:-}" = 1 ] && exit "$code"  # for tests: one pass
  if [ $(( $(date +%s) - started )) -lt "$QUICK_EXIT" ]; then resume=0; else resume=1; fi
  echo "[$(date -Is)] claude exited with $code; restarting in 5s"
  sleep 5
done
