"""The way in from Telegram when the router polls the Bot API itself: "frontdesk": "router" in
config.json, in place of the front desk, a Claude Code session that hosts the Telegram plugin
(one claude and two Bun processes, 300 to 400 MB, and a model turn for every message). A thread
long-polls getUpdates and does what the patched plugin does: it keeps strangers out and hands out
pairing codes (access.py), answers /start and /status and a stranger's /help, fetches photos,
and queues everything else for the loop as the Inbound the plugin would have passed on
(updates.py). Telegram keeps what was sent while nothing polled, so a restart loses nothing.

state/telegram-updates.json holds {"offset": the next update to ask for, "saved": when, "heard": when
Telegram last answered}. The router's loop saves it once it has routed what came, and at least once a
minute while it runs, network or not, so a restart can tell how long the router itself was down; it
also starts the thread again should it stop. Only one program may poll a bot: the thread holds a lock
(state/telegram-updates.lock) against a second router, `assistant install` turns the plugin off for the
repo's folder, and when the plugin's poller runs anyway or Telegram says another poller has the token
(409), the owner is told."""
import fcntl
import queue
import re
import threading
import time
import traceback

from multiclaude import access, addons, bot, jsonfile, paths, telegram
from multiclaude.log import log
from multiclaude.router import poller, updates

STATE = paths.STATE / "telegram-updates.json"
LOCK = paths.STATE / "telegram-updates.lock"
INBOX = paths.TELEGRAM_DIR / "inbox"  # where photos are kept for the assistants, as the plugin keeps them
LONG_POLL = 25            # seconds Telegram holds a getUpdates with nothing to give
HEARTBEAT = 60            # the state is saved at least this often
RESUME_WITHIN = 60 * 60   # after the router was down longer, what was sent meanwhile is skipped rather than delivered late
RETRY_FIRST, RETRY_MAX = 1, 15  # seconds between attempts while Telegram can't be polled, growing
LOCK_RETRY = 30
APPROVALS_EVERY = 2       # how often to look for people `assistant pair` approved
FRONT_DESK_EVERY = 60     # how often to look for the front desk's plugin still polling
THREAD_RETRY = 30         # at least this long between starts of the polling thread
TELL_DYING_AFTER = 3      # starts again within DYING_WITHIN before the owner hears that it keeps stopping
DYING_WITHIN = 60 * 60
DOWNLOAD_TIMEOUT = 60
# What the thread asks Telegram for. Reactions come only to a poller that asks for them, and
# edited messages carry a live location's updates (the locations add-in).
ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "message_reaction"]
COMMAND_RE = re.compile(r"/(start|help|status)(?:@(\w+))?(?:\s|$)")
# The menu of a private chat nobody is paired in; a paired person's chat has the router's (about.COMMANDS).
STRANGER_COMMANDS = [{"command": "start", "description": "Welcome and setup guide"},
                     {"command": "help", "description": "What this bot can do"},
                     {"command": "status", "description": "Check your pairing status"}]
START = ("This bot gives each person their own Claude Code assistant.\n\n"
         "To pair:\n1. Send me any message: you'll get a 6-character code\n"
         "2. The owner of the machine runs: assistant pair <code>\n\n"
         "After that, what you send here reaches your assistant.")
STRANGER_HELP = ("What you send here goes to your own assistant once you're paired.\n\n"
                 "/start: how to pair\n/status: check your pairing")
PAIR = "{lead}. The owner of the machine runs:\n\nassistant pair {code}"
NOT_PAIRED = "Not paired. Send me a message to get a pairing code."
CONFLICT = ("📡 {bot} can't hear Telegram reliably: another program is polling with its bot token, and each "
            "message reaches only one of them. With `\"frontdesk\": \"router\"` in `config.json` only the router "
            "may. If the front desk's Telegram plugin still runs, run `assistant install` and then `assistant restart` "
            "to turn it off.")
FRONT_DESK_POLLS = ("📡 {bot} can't hear Telegram reliably: the front desk's Telegram plugin still polls it, as the router "
                    "does with `\"frontdesk\": \"router\"` in `config.json`, and each message reaches only one of them. "
                    "Restarting the router alone doesn't stop the front desk: run `assistant install` and then "
                    "`assistant restart` to finish the switch.")
DYING = ("📡 The router's polling of Telegram keeps stopping: it started it again {times} times in the last hour, so "
         "messages may come late. See why with `journalctl -u assistant-watcher -n 100`.")


def saved_state():
    """(the next update to ask for, when the router saved it, when Telegram last answered), None for each unknown."""
    kept = jsonfile.read_json(STATE, {})
    kept = kept if isinstance(kept, dict) else {}
    offset = kept.get("offset")
    saved, heard = (kept[key] if isinstance(kept.get(key), (int, float)) else None for key in ("saved", "heard"))
    return (offset if isinstance(offset, int) else None), saved, heard


def plugin_polling():
    """Whether the Telegram plugin's poller runs: its pid file names a live server.ts."""
    try:
        pid = int(poller.pid_file().read_text().strip())
        return b"server.ts" in (paths.PROC / str(pid) / "cmdline").read_bytes()
    except (OSError, ValueError):
        return False


class TelegramEntrance:
    def __init__(self, tell_owner=lambda text: None):
        self.inbox = queue.Queue()  # router.inbound.Inbound for the loop to route, and after each batch the offset it reached
        self.tell_owner = tell_owner
        self.api = telegram.BotApi(timeout=LONG_POLL + 15)  # its own client: a long poll outlasts the router's timeouts
        self.username = None        # the bot's, to tell its own commands and mentions
        self.offset, saved, heard = saved_state()
        # After the router was down long, what Telegram kept is skipped. A first start has nothing saved and takes it all.
        self.skip_kept = saved is not None and time.time() - saved > RESUME_WITHIN
        self.taken = self.offset    # how far the loop has taken what came, saved once it has routed that
        self.heard = heard or 0.0   # when Telegram last answered a getUpdates
        self.saved = (self.offset, 0.0)  # what was last written to STATE, and when
        self.conflict_told = False
        self.next_approvals = 0.0
        self.next_front_desk = time.time() + FRONT_DESK_EVERY  # a front desk stopped by `assistant restart` may take a moment
        self.lock = None
        self.thread = None
        self.started = 0.0
        self.restarts = []          # when the thread was started again, within DYING_WITHIN
        self.dying_told = False

    # --- the thread ---

    def start(self, now=None):
        self.thread = threading.Thread(target=self.run, name="telegram-entrance", daemon=True)
        self.started = time.time() if now is None else now
        self.thread.start()

    def run(self):
        try:
            self.hold_lock()
            self.poll_on()
        except Exception:
            log(f"polling Telegram stopped:\n{traceback.format_exc().rstrip()}")  # the loop starts it again (keep_polling)

    def poll_on(self):
        wait = RETRY_FIRST
        while True:
            try:
                self.poll()
                wait = RETRY_FIRST
                continue
            except telegram.TelegramError as e:
                self.failed(e, wait)
            except Exception:
                log(f"polling Telegram failed:\n{traceback.format_exc().rstrip()}")
            time.sleep(wait)
            wait = min(wait * 2, RETRY_MAX)

    def hold_lock(self):
        """One router polls at a time: wait for the lock a router still running holds."""
        if self.lock:
            return  # the thread started again: this router has it already
        paths.STATE.mkdir(parents=True, exist_ok=True)
        lock = open(LOCK, "w")
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.lock = lock
                return
            except OSError:
                log("another router is polling Telegram; waiting for it to stop")
                time.sleep(LOCK_RETRY)

    def failed(self, error, wait):
        if "Conflict" in str(error):
            log(f"{error}; trying again in {wait}s")
            if not self.conflict_told:
                self.conflict_told = True
                self.tell_owner(CONFLICT.format(bot=bot.name()))
        elif not isinstance(error, telegram.Unreachable) or wait == RETRY_FIRST:
            log(f"{error}; trying again in {wait}s")  # an outage is said once: the network check reports it

    def poll(self):
        """One getUpdates: queue what came for the loop, then how far that was."""
        if self.username is None:
            self.introduce()
        if self.skip_kept:
            self.skip()
        found = self.api.call("getUpdates", offset=self.offset, timeout=LONG_POLL, allowed_updates=ALLOWED_UPDATES)
        self.heard = time.time()
        for update in found or []:
            try:
                self.take(update)
            except Exception:
                log(f"taking update {update.get('update_id')} failed:\n{traceback.format_exc().rstrip()}")
            self.offset = update["update_id"] + 1
        if self.offset is not None:
            self.inbox.put(self.offset)

    def introduce(self):
        """Learn the bot's own username, and give the chats of strangers their short menu."""
        self.username = (self.api.call("getMe") or {}).get("username") or ""
        log(f"polling Telegram as @{self.username}")
        telegram.later(telegram.api, "setMyCommands", commands=STRANGER_COMMANDS, scope={"type": "all_private_chats"})

    def skip(self):
        """Let go of everything Telegram kept: asking for the last update alone confirms the rest."""
        last = self.api.call("getUpdates", offset=-1, timeout=0, allowed_updates=ALLOWED_UPDATES)
        if last:
            self.offset = last[-1]["update_id"] + 1
            self.inbox.put(self.offset)
        self.skip_kept = False
        log("the router was down too long; messages sent meanwhile are skipped")

    def save(self, now):
        offset, at = self.saved
        if not self.lock or (self.taken == offset and now - at < HEARTBEAT):
            return  # a router waiting for another's lock leaves the state to that one
        try:
            jsonfile.write_json(STATE, {"offset": self.taken, "saved": now, "heard": self.heard})
        except OSError as e:
            log(f"can't save {STATE}: {e}")
            return
        self.saved = (self.taken, now)

    # --- what came ---

    def take(self, update):
        if message := update.get("message"):
            self.message(message)
        elif edited := update.get("edited_message"):
            if self.paired_in_private(edited.get("chat"), edited.get("from")):
                addons.call("telegram_edited_message", edited)
        elif query := update.get("callback_query"):
            self.tapped(query)
        elif reaction := update.get("message_reaction"):
            self.reacted(reaction)

    def paired_in_private(self, chat, sender):
        return bool(sender) and (chat or {}).get("type") == "private" and access.allowed(access.read(), sender["id"])

    def message(self, message):
        chat, sender = message.get("chat") or {}, message.get("from")
        if not sender:
            return
        paired = self.paired_in_private(chat, sender)
        if self.command(message, paired):
            return
        found = updates.content(message) or self.from_addons(message, paired)
        if not found:
            return  # a kind nothing passes on (a contact, a poll, a dice): no code for it either, as with the plugin
        verdict = access.gate(message, self.username)
        if verdict.action == "pair":
            self.reply(chat, PAIR.format(lead="Still pending" if verdict.resend else "Pairing required", code=verdict.code))
        if verdict.action != "deliver":
            return
        text, attrs, photo = found
        if photo and (path := self.fetch(photo)):
            attrs = {**attrs, "image_path": path}
        self.acknowledge(message, verdict.access)
        self.inbox.put(updates.inbound(message, text, attrs))

    def from_addons(self, message, paired):
        """(text, attributes, None) from the first add-in that takes the message (telegram_message), or None."""
        taken = next((found for found in addons.call("telegram_message", message, paired) if found), None)
        return (*taken, None) if taken else None

    def command(self, message, paired):
        """Answer /start and /status, and a stranger's /help, as the plugin does. Whether the
        message was dealt with: a paired person's /help goes on to the router's own."""
        asked = COMMAND_RE.match(message.get("text") or "")
        if not asked or (asked[2] and self.username and asked[2].lower() != self.username.lower()):
            return False
        now_allowed = access.current()
        chat, sender = message["chat"], str(message["from"]["id"])
        if (chat.get("type") != "private" or now_allowed["dmPolicy"] == "disabled"
                or (now_allowed["dmPolicy"] == "allowlist" and not paired)):
            return True  # no answer in a group, where a code would show to everyone, and none to strangers
        if asked[1] == "start":
            self.reply(chat, START)
        elif asked[1] == "help":
            if paired:
                return False
            self.reply(chat, STRANGER_HELP)
        elif paired:
            username = message["from"].get("username")
            self.reply(chat, f"Paired as {'@' + username if username else sender}.")
        elif code := access.pending_code(now_allowed, sender):
            self.reply(chat, PAIR.format(lead="Pending pairing", code=code))
        else:
            self.reply(chat, NOT_PAIRED)
        return True

    def reply(self, chat, text):
        telegram.later(telegram.api, "sendMessage", chat_id=chat["id"], text=text)

    def acknowledge(self, message, allowed_now):
        """React to a message that is going on, when the owner set an ackReaction (`assistant pair` sets 👀)."""
        emoji = allowed_now.get("ackReaction")
        if emoji and message.get("message_id") is not None:
            telegram.later(telegram.api, "setMessageReaction", chat_id=message["chat"]["id"],
                           message_id=message["message_id"], reaction=[{"type": "emoji", "emoji": emoji}])

    def fetch(self, photo):
        """Save a photo for the assistant to look at; its path, or None if it couldn't be fetched."""
        try:
            file_path = (self.api.call("getFile", file_id=photo["file_id"]) or {}).get("file_path")
            if not file_path:
                return None
            INBOX.mkdir(parents=True, exist_ok=True)
            extension = file_path.rsplit(".", 1)[1] if "." in file_path else "jpg"
            path = INBOX / f"{int(time.time() * 1000)}-{photo.get('file_unique_id', 'photo')}.{extension}"
            self.api.download(file_path, path, timeout=DOWNLOAD_TIMEOUT)
        except (telegram.TelegramError, OSError) as e:
            log(f"photo download failed: {e}")
            return None
        return str(path)

    def tapped(self, query):
        """A tap on a button. Those of `tg ask` and `tg checklist` (ask:<n>) go on, from a paired
        person in their private chat; every tap is answered, so the button stops spinning."""
        telegram.later(telegram.api, "answerCallbackQuery", callback_query_id=query["id"])
        message = query.get("message") or {}
        if updates.ASK_RE.fullmatch(query.get("data") or "") and self.paired_in_private(message.get("chat"), query.get("from")):
            self.inbox.put(updates.tap(query))

    def reacted(self, reaction):
        """An emoji a paired person added to a message in their private chat."""
        if not self.paired_in_private(reaction.get("chat"), reaction.get("user")):
            return
        emoji, custom = updates.added_reactions(reaction)
        added = " ".join(emoji + self.custom_emoji(custom))
        if added:
            self.inbox.put(updates.reacted(reaction, added))

    def custom_emoji(self, ids):
        """The emoji each Premium custom emoji stands for, as its sticker says."""
        if not ids:
            return []
        try:
            stickers = self.api.call("getCustomEmojiStickers", custom_emoji_ids=ids) or []
        except telegram.TelegramError:
            stickers = []
        stands_for = {sticker.get("custom_emoji_id"): sticker.get("emoji") for sticker in stickers}
        return [stands_for.get(i) or "(custom emoji)" for i in ids]

    # --- for the router's loop ---

    def waiting(self):
        """The messages that arrived since the last call."""
        found = []
        while True:
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                return found
            if isinstance(item, int):
                self.taken = item
            else:
                found.append(item)

    def tick(self, now):
        """Once the loop has routed what waiting() gave: keep the thread polling, save how far that was, watch for
        the front desk's plugin, and tell the people `assistant pair` approved that they're paired."""
        self.keep_polling(now)
        self.save(now)
        self.watch_front_desk(now)
        if now < self.next_approvals:
            return
        self.next_approvals = now + APPROVALS_EVERY
        for sender in access.take_approved():
            telegram.later(telegram.api, "sendMessage", chat_id=sender, text=f"Paired! Say hi to {bot.name()}.")

    def keep_polling(self, now):
        """Start the thread again if it stopped, and tell the owner once if it keeps stopping."""
        if self.thread is None or self.thread.is_alive() or now - self.started < THREAD_RETRY:
            return
        self.restarts = [at for at in self.restarts if now - at < DYING_WITHIN] + [now]
        log("the thread polling Telegram stopped; starting it again")
        if len(self.restarts) >= TELL_DYING_AFTER and not self.dying_told:
            self.dying_told = True
            self.tell_owner(DYING.format(times=len(self.restarts)))
        self.start(now)

    def watch_front_desk(self, now):
        """Tell the owner once if the front desk's plugin still polls: switching to the router and restarting
        only the router leaves it running."""
        if self.conflict_told or now < self.next_front_desk:
            return
        self.next_front_desk = now + FRONT_DESK_EVERY
        if plugin_polling():
            self.conflict_told = True
            log("the front desk's Telegram plugin still polls Telegram")
            self.tell_owner(FRONT_DESK_POLLS.format(bot=bot.name()))
