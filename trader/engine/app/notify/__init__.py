"""User notifications: Telegram + Web Push, fed by the audit trail.

  service.py   emit() / notify_event() - preferences, quiet hours, dedupe, fan-out
  texts.py     Persian wording and the event -> category mapping
  telegram.py  Bot API channel (+ chat-id discovery)
  webpush.py   VAPID Web Push channel (+ key management)
  ipwatch.py   public-IP watchdog (the Binance key is IP-restricted)
"""
