# Caster-bot-

Discord bot for esports caster request management.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Configure environment variables:
   - `DISCORD_BOT_TOKEN` (required)
   - `DISCORD_GUILD_ID` (optional; faster slash-command sync for one guild)
   - `CASTER_ROLE_ID` (optional; role to ping/check as caster role)
   - `CASTER_BOT_DB` (optional; sqlite db path, default `caster_bot.db`)
3. Run the bot:
   ```bash
   python bot.py
   ```

## Commands

- `/requestcast` - Create a caster request (event type, event time, optional notes)
- `/waitlist` - View current waitlist (staff)
- `/removerequest` - Remove request (staff)
- `/assigncaster` - Manually assign caster (staff)
- `/closecastrequest` - Close request (staff)
- `/casterstatus` - Post readiness controls and queue status (staff)

The bot persists request queue state, cooldowns, availability, readiness, assignment history, and completion records in SQLite.
