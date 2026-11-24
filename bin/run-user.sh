#!/usr/bin/env bash
# Runs one user's assistant inside the "u-<name>" tmux session (socket -L
# assistant), started by `assistant start-users`, and by the router with "fresh" for
# /restart. Keeps a Claude Code session open in that user's workspace, restarting it
# if it exits. The router types the user's messages into it, and it replies with `tg`
# (or an add-in's command, for an app the add-in brings).
#
# A person with an "isolated" entry in users.json (workspace, Claude config dir, Linux user) runs
# as that Linux user through sudo, with its own login, no bot env and no `tg`, and with the
# permission mode set there instead of bypassPermissions.
set -u

name=${1:?usage: run-user.sh NAME [fresh]}
# shellcheck source-path=SCRIPTDIR source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
WS=$(user_workspace "$name")
SESSIONS=$(user_project_dir "$name")
QUICK_EXIT=30  # a session that dies sooner than this after starting isn't resumed next time
lu=$(user_isolated "$name" linux_user)
mode=bypassPermissions

if [ -n "$lu" ]; then
  mode=$(user_isolated "$name" permission_mode)
  claude_dir=$(user_isolated "$name" claude_config_dir)
  lhome=$(getent passwd "$lu" | cut -d: -f6)
  [ -n "$lhome" ] || { echo "no Linux user $lu"; sleep 60; exit 1; }
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
  args=(--permission-mode "$mode" --name "$name" --settings "$TELEGRAM_PLUGIN_OFF")
  # Pick up the previous conversation so context survives restarts. If that
  # session dies right away (e.g. it's corrupt), start fresh next time.
  if [ "$resume" = 1 ] && compgen -G "$SESSIONS/*.jsonl" >/dev/null; then
    args+=(--continue)
  fi
  started=$(date +%s)
  if [ -n "$lu" ]; then
    # The workspace is the other user's: cd into it as them. The absolute /usr/bin/env is the one
    # command sudoers lets this user run as them.
    sudo -n -u "$lu" /usr/bin/env HOME="$lhome" CLAUDE_CONFIG_DIR="$claude_dir" \
      PATH="$lhome/.local/bin:$lhome/.bun/bin:/usr/local/bin:/usr/bin:/bin" \
      TG_CHAT_ID= ASSISTANT_USER="$name" CLAUDE_CODE_SANDBOXED=1 \
      bash -c 'cd "$1" && exec claude "${@:2}"' _ "$WS" "${args[@]}"
  else
    claude "${args[@]}"
  fi
  code=$?
  [ "${RUN_USER_ONCE:-}" = 1 ] && exit "$code"  # for tests: one pass
  if [ $(( $(date +%s) - started )) -lt "$QUICK_EXIT" ]; then resume=0; else resume=1; fi
  echo "[$(date -Is)] claude exited with $code; restarting in 5s"
  sleep 5
done
