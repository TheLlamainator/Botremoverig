# Instagram Bot Follower Remover

A CLI tool that logs into your Instagram account, scans your followers for
accounts that look like bots, and lets you review and remove them in
controlled batches.

Built on [instagrapi](https://github.com/subzeroid/instagrapi).

## ⚠️ Before you use this

- Use a strong, unique password and consider that automating logins can
  trigger Instagram's automation/spam detection. Use at your own risk and
  prefer running it occasionally rather than constantly.
- Always review the flagged list before removing anything. The heuristics
  are best-effort and can produce false positives (e.g. real people with
  minimal profiles).
- Start with `--dry-run` the first time to see what would happen.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python3 bot_remover.py
```

You'll be prompted for your username and password (and a 2FA code if your
account has two-factor authentication enabled). After the first successful
login, a session is saved to `ig_cache/session.json` so you won't need to log
in again on future runs.

### What it does

1. **Login** - via `instagrapi`, with 2FA/challenge support and session reuse.
2. **Fetch followers** - downloads your full followers list, then fetches
   detailed profile info for each one (cached in `ig_cache/` so you can
   resume if interrupted).
3. **Engagement whitelist** - checks your most recent posts for likes and
   comments; anyone who has ever engaged is never flagged for removal.
4. **Bot detection** - flags an account if enough of these are true:
   - No profile picture
   - Fewer than 3 posts
   - Following-to-follower ratio over 10:1
   - Username looks randomly generated (lots of digits / no vowels)
   - No bio
   - Account appears very new (estimated from its numeric ID - approximate)
   - Private account with zero posts

   By default an account needs **3+** of these signals to be flagged
   (`--threshold`).
5. **Review** - prints every flagged account and why, before anything is
   removed.
6. **Batch removal** - approve removals in batches (default 50,
   `--batch-size`), with a random 3-8 second delay (`--delay-min`/
   `--delay-max`) between each removal to avoid rate limiting. Every removal
   is logged with a timestamp to `removed_followers_log.txt`.

### Safety behavior

- If Instagram returns a rate-limit response, the script pauses for 15
  minutes and then resumes automatically.
- If 3 errors happen in a row while removing followers, the script stops and
  tells you to check your account before continuing.
- Press **Ctrl+C** at any time to stop cleanly after the current step;
  progress (cached profile data, engagement whitelist, removal log) is saved
  so you can resume later.

### Useful flags

```bash
python3 bot_remover.py --dry-run                  # preview only, removes nothing
python3 bot_remover.py --threshold 4              # require more signals before flagging
python3 bot_remover.py --batch-size 25            # smaller review batches
python3 bot_remover.py --delay-min 5 --delay-max 12
python3 bot_remover.py --posts-to-check 50        # scan more posts for engagement
python3 bot_remover.py --reset-cache              # refetch all follower/engagement data

# Only scan the ~6000 followers that came after a known "boundary" account
# in Instagram's follower ordering (most-recently-followed first), e.g. if
# you know a bot wave happened right before a specific real follower:
python3 bot_remover.py --anchor-username leah_bytes --anchor-window 6000
```

### Anchor-based scanning

`--anchor-username` finds that account in your followers list and restricts
the scan to the accounts that follow it in Instagram's ordering (which, in
practice, tends to be most-recently-followed first). Combined with
`--anchor-window`, this lets you target a suspected bot wave that happened
just before/after a specific real follower, without scanning your entire
followers list. Note: this ordering is observed behavior, not something
Instagram documents or guarantees.

## Files created

- `ig_cache/session.json` - saved login session
- `ig_cache/followers_info.json` - cached follower profile data
- `ig_cache/interaction_whitelist.json` - accounts that liked/commented on your posts
- `removed_followers_log.txt` - timestamped log of every removal

None of these are committed to git (see `.gitignore`).
