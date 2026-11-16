"""
Telegram notifications through the bot and chat already configured in
Maintainerr (Settings -> Notifications -> Telegram), so no extra secrets
are stored here.
"""

import json
import logging
import urllib.parse
import urllib.request

MAX_LEN = 4000  # Telegram limit is 4096 characters


def html(text) -> str:
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def gib(size: float) -> str:
    return f"{size / 1024 ** 3:.1f} GiB"


def _agent(maintainerr_url: str) -> dict:
    url = maintainerr_url.rstrip('/') + '/api/notifications/configurations'
    with urllib.request.urlopen(url, timeout=30) as r:
        agents = json.loads(r.read())
    agent = next((a for a in agents if a.get('agent') == 'telegram' and a.get('enabled')), None)
    if not agent:
        raise RuntimeError('no enabled Telegram agent in Maintainerr')
    return agent['options']


def send(maintainerr_url: str, text: str) -> None:
    """Send an HTML-formatted message; long messages are cut at a line boundary."""
    if len(text) > MAX_LEN:
        cut = text.rfind('\n', 0, MAX_LEN - 40)
        text = text[:cut if cut > 0 else MAX_LEN - 40] + '\n… (truncado, ver log)'
    opts = _agent(maintainerr_url)
    data = urllib.parse.urlencode({
        'chat_id': opts['chatId'], 'text': text, 'parse_mode': 'HTML',
        'disable_notification': 'true' if opts.get('sendSilently') else 'false',
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{opts['botAuthToken']}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        if not json.loads(r.read()).get('ok'):
            raise RuntimeError('Telegram API returned not ok')
    logging.info("Telegram summary sent (%d chars)", len(text))
