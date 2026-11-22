"""The service-notify add-in: local services (not Claude sessions) message the owner on Telegram
through the bot, with pictures and buttons, and hear the presses back, with no assistant in the
loop. See addons/service-notify/README.md and docs/service-notify/DESIGN.md.

Its hooks: question_tapped takes the taps on a service's buttons (taps.py), and on_tick starts
the local API (api.py) and sweeps expired buttons and presses no service took, in the
background, every few seconds."""
import threading

from multiclaude.addons.service_notify import api, taps
from multiclaude.log import log

SWEEP_EVERY = 5
_sweeping = threading.Lock()
_next_sweep = [0.0]


def question_tapped(user, tap, path, ask, now):
    return taps.tapped(user, tap, path, ask, now)


def on_tick(desk, now):
    api.start_once(desk, now)
    if now >= _next_sweep[0] and _sweeping.acquire(blocking=False):
        _next_sweep[0] = now + SWEEP_EVERY
        threading.Thread(target=_sweep, args=(now,), daemon=True, name="service-notify sweep").start()


def _sweep(now):
    try:
        taps.sweep(now)
    except Exception as e:
        log(f"service-notify: the sweep failed: {e!r}")
    finally:
        _sweeping.release()
