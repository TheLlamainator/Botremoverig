#!/usr/bin/env python3
"""Instagram Bot Follower Remover.

A CLI tool that logs into an Instagram account, scans followers for accounts
that look like bots, and lets you review and remove them in controlled batches.

Built on top of instagrapi: https://github.com/subzeroid/instagrapi
"""

import argparse
import json
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, timezone
from getpass import getpass

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    ClientError,
    LoginRequired,
    PleaseWaitFewMinutes,
    TwoFactorRequired,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CACHE_DIR = "ig_cache"
SESSION_FILE = os.path.join(CACHE_DIR, "session.json")
FOLLOWERS_CACHE = os.path.join(CACHE_DIR, "followers_info.json")
WHITELIST_CACHE = os.path.join(CACHE_DIR, "interaction_whitelist.json")
REMOVED_LOG = "removed_followers_log.txt"

DEFAULT_BATCH_SIZE = 50
DEFAULT_DELAY_RANGE = (3, 8)
DEFAULT_BOT_THRESHOLD = 3
DEFAULT_NEW_ACCOUNT_DAYS = 180
DEFAULT_POSTS_TO_CHECK = 30
RATE_LIMIT_PAUSE_SECONDS = 15 * 60
MAX_CONSECUTIVE_ERRORS = 3

# Rough formula for decoding the creation timestamp embedded in modern
# Instagram numeric IDs (Snowflake-style). Accounts created before this
# scheme was introduced (small pks) cannot be dated this way, so the age
# check is skipped for them. Treat the result as an estimate, not a fact.
INSTAGRAM_ID_EPOCH_MS = 1314220021721
MIN_DATEABLE_PK = 2 ** 32

shutdown_requested = False


def handle_sigint(signum, frame):
    global shutdown_requested
    if shutdown_requested:
        # Second Ctrl+C: exit immediately.
        print("\nForce quitting.")
        sys.exit(1)
    shutdown_requested = True
    print("\n\n[!] Ctrl+C received. Finishing the current step safely and stopping...")


def interruptible_sleep(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if shutdown_requested:
            return
        time.sleep(min(1, end - time.time()))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def to_dict(model):
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model.dict()


def log_line(message):
    timestamp = datetime.now().isoformat(timespec="seconds")
    with open(REMOVED_LOG, "a") as f:
        f.write(f"{timestamp} | {message}\n")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def login():
    def challenge_code_handler(username, choice):
        print(f"\nInstagram needs to verify it's you (sent a code via {choice}).")
        return input("Enter the verification code: ").strip()

    username = input("Instagram username: ").strip()

    cl = Client()
    cl.challenge_code_handler = challenge_code_handler
    session_ok = False
    if os.path.exists(SESSION_FILE):
        try:
            cl.load_settings(SESSION_FILE)
            cl.username = username
            cl.get_timeline_feed()  # validates the saved session
            session_ok = True
            print(f"Resumed saved session for @{username}.")
        except Exception:
            session_ok = False

    if not session_ok:
        password = getpass("Instagram password: ")
        try:
            try:
                cl.login(username, password)
            except TwoFactorRequired:
                code = input("Two-factor code: ").strip()
                cl.login(username, password, verification_code=code)
            except ChallengeRequired:
                cl.challenge_resolve(cl.last_json)
                cl.login(username, password)
        except Exception as e:
            print(f"\n[ALERT] Login failed: {e}")
            print("Stopping here for safety. Double-check your credentials and try again.")
            sys.exit(1)

        cl.dump_settings(SESSION_FILE)
        print(f"Logged in as @{username}. Session saved to {SESSION_FILE}.")

    return cl


# ---------------------------------------------------------------------------
# Rate-limit-aware API wrapper
# ---------------------------------------------------------------------------
def safe_api_call(func, *args, max_retries=5, **kwargs):
    for attempt in range(1, max_retries + 1):
        if shutdown_requested:
            raise KeyboardInterrupt
        try:
            return func(*args, **kwargs)
        except PleaseWaitFewMinutes:
            print(
                f"\n[!] Instagram is rate-limiting requests. Pausing for "
                f"{RATE_LIMIT_PAUSE_SECONDS // 60} minutes (attempt {attempt}/{max_retries})..."
            )
            interruptible_sleep(RATE_LIMIT_PAUSE_SECONDS)
        except ClientError as e:
            msg = str(e).lower()
            if "wait" in msg or "rate" in msg or "429" in msg or "throttl" in msg:
                print(
                    f"\n[!] Possible rate limiting ({e}). Pausing for "
                    f"{RATE_LIMIT_PAUSE_SECONDS // 60} minutes (attempt {attempt}/{max_retries})..."
                )
                interruptible_sleep(RATE_LIMIT_PAUSE_SECONDS)
            else:
                raise
        if shutdown_requested:
            raise KeyboardInterrupt
    raise RuntimeError("Repeated rate limiting - giving up on this request.")


# ---------------------------------------------------------------------------
# Bot heuristics
# ---------------------------------------------------------------------------
def estimate_account_age_days(pk):
    """Very rough estimate of account age based on the numeric user id.

    Returns None when the account predates the timestamp-based id scheme
    (or anything looks off), in which case the "new account" check is skipped.
    """
    try:
        pk = int(pk)
    except (TypeError, ValueError):
        return None

    if pk < MIN_DATEABLE_PK:
        return None

    created_ms = (pk >> 23) + INSTAGRAM_ID_EPOCH_MS
    try:
        created = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None

    now = datetime.now(tz=timezone.utc)
    if created > now or created.year < 2011:
        return None

    return (now - created).total_seconds() / 86400


def username_looks_random(username):
    name = (username or "").lower()
    if not name:
        return False

    digit_count = sum(c.isdigit() for c in name)
    digit_ratio = digit_count / len(name)

    trailing_digits = 0
    m = re.search(r"\d+$", name)
    if m:
        trailing_digits = len(m.group())

    alpha_only = re.sub(r"[^a-z]", "", name)
    vowels = sum(c in "aeiou" for c in alpha_only)
    vowel_ratio = (vowels / len(alpha_only)) if alpha_only else 1

    if digit_ratio > 0.3 and len(name) > 5:
        return True
    if trailing_digits >= 5:
        return True
    if len(alpha_only) >= 6 and vowel_ratio < 0.15:
        return True
    return False


def compute_bot_flags(info, new_account_days):
    """Return a list of human-readable reasons this account looks like a bot."""
    reasons = []

    follower_count = info.get("follower_count") or 0
    following_count = info.get("following_count") or 0
    media_count = info.get("media_count") or 0
    is_private = bool(info.get("is_private"))
    biography = (info.get("biography") or "").strip()
    username = info.get("username", "")
    profile_pic_url = info.get("profile_pic_url") or ""
    has_anonymous_pic = bool(info.get("has_anonymous_profile_picture"))

    if has_anonymous_pic or not profile_pic_url:
        reasons.append("No profile picture")

    if media_count < 3:
        reasons.append(f"Very few posts ({media_count})")

    if following_count > 0 and follower_count > 0 and following_count / follower_count > 10:
        reasons.append(
            f"Following/follower ratio over 10:1 ({following_count}/{follower_count})"
        )
    elif follower_count == 0 and following_count > 50:
        reasons.append(f"Zero followers but following {following_count} accounts")

    if username_looks_random(username):
        reasons.append("Username looks randomly generated")

    if not biography:
        reasons.append("No bio")

    age_days = estimate_account_age_days(info.get("pk"))
    if age_days is not None and age_days < new_account_days:
        reasons.append(f"Very new account (~{int(age_days)} days old, estimated)")

    if is_private and media_count == 0:
        reasons.append("Private account with no posts")

    return reasons


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------
def fetch_followers(cl):
    print("\nFetching your followers list (this can take a while for large accounts)...")
    followers = safe_api_call(cl.user_followers, cl.user_id)
    print(f"Found {len(followers)} followers.")
    return followers


def select_by_anchor(followers, anchor_username, window_size):
    """Restrict the followers dict to a window of accounts that appear after
    a given "anchor" account in Instagram's follower ordering.

    Instagram's followers endpoint tends to return followers most-recently-followed
    first (this is observed behavior, not a documented guarantee). If a known-good
    account marks the boundary of a bot wave, the suspected bots are the accounts
    that follow it in that ordering.

    Returns (selected_followers, anchor_pk) or (None, None) if the anchor
    username isn't found in the followers list.
    """
    items = list(followers.items())
    anchor_index = None
    anchor_pk = None
    for i, (pk, short) in enumerate(items):
        if (short.username or "").lower() == anchor_username.lower():
            anchor_index = i
            anchor_pk = pk
            break

    if anchor_index is None:
        return None, None

    start = anchor_index + 1
    end = start + window_size
    selected = dict(items[start:end])
    return selected, anchor_pk


def fetch_follower_details(cl, followers, cache):
    pks = list(followers.keys())
    total = len(pks)
    already_cached = sum(1 for pk in pks if str(pk) in cache)
    if already_cached:
        print(f"Using cached profile data for {already_cached}/{total} followers.")

    for i, pk in enumerate(pks, start=1):
        if shutdown_requested:
            print("\nStopping profile lookups (Ctrl+C). Progress saved - rerun to resume.")
            break

        key = str(pk)
        if key in cache:
            continue

        try:
            info = safe_api_call(cl.user_info_v1, pk)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  [!] Could not fetch @{followers[pk].username}: {e}")
            continue

        cache[key] = to_dict(info)

        if i % 20 == 0 or i == total:
            save_json(FOLLOWERS_CACHE, cache)
            print(f"  ...looked up {i}/{total} followers")

        interruptible_sleep(random.uniform(1, 2.5))

    save_json(FOLLOWERS_CACHE, cache)
    return cache


def build_interaction_whitelist(cl, posts_to_check, cached_whitelist):
    if cached_whitelist is not None:
        print(f"Using cached list of {len(cached_whitelist)} accounts that engaged with your posts.")
        return set(cached_whitelist)

    print(f"\nChecking your last {posts_to_check} posts for likes/comments "
          f"(these accounts will never be removed)...")
    whitelist = set()

    try:
        medias = safe_api_call(cl.user_medias, cl.user_id, posts_to_check)
    except Exception as e:
        print(f"  [!] Could not fetch your posts ({e}). Skipping engagement check.")
        return whitelist

    for i, media in enumerate(medias, start=1):
        if shutdown_requested:
            print("\nStopping engagement check (Ctrl+C).")
            break

        try:
            likers = safe_api_call(cl.media_likers, media.id)
            whitelist.update(u.pk for u in likers)
        except Exception as e:
            print(f"  [!] Could not fetch likes for a post: {e}")

        try:
            comments = safe_api_call(cl.media_comments, media.id)
            whitelist.update(c.user.pk for c in comments if c.user)
        except Exception as e:
            print(f"  [!] Could not fetch comments for a post: {e}")

        print(f"  ...checked {i}/{len(medias)} posts")
        interruptible_sleep(random.uniform(1, 2.5))

    save_json(WHITELIST_CACHE, list(whitelist))
    return whitelist


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyze(followers, details_cache, whitelist, threshold, new_account_days):
    flagged = []
    skipped_no_data = 0
    skipped_whitelisted = 0

    for pk, short in followers.items():
        info = details_cache.get(str(pk))
        if info is None:
            skipped_no_data += 1
            continue

        if int(pk) in whitelist:
            skipped_whitelisted += 1
            continue

        reasons = compute_bot_flags(info, new_account_days)
        if len(reasons) >= threshold:
            flagged.append({
                "pk": pk,
                "username": info.get("username", getattr(short, "username", "")),
                "full_name": info.get("full_name", "") or "",
                "score": len(reasons),
                "reasons": reasons,
            })

    flagged.sort(key=lambda a: -a["score"])
    return flagged, skipped_no_data, skipped_whitelisted


def print_flagged_summary(flagged):
    if not flagged:
        print("\nNo accounts were flagged as likely bots. Nothing to do.")
        return

    print(f"\n{'=' * 70}")
    print(f"Flagged {len(flagged)} account(s) as likely bots:")
    print(f"{'=' * 70}")
    for acc in flagged:
        print(f"\n@{acc['username']}  ({acc['full_name']})  -- score {acc['score']}")
        for reason in acc["reasons"]:
            print(f"    - {reason}")
    print(f"\n{'=' * 70}")


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------
def remove_accounts(cl, flagged, batch_size, delay_range, dry_run):
    total = len(flagged)
    removed_count = 0
    consecutive_errors = 0

    for batch_start in range(0, total, batch_size):
        if shutdown_requested:
            print("\nStopping before the next batch (Ctrl+C).")
            break

        batch = flagged[batch_start:batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size

        print(f"\n--- Batch {batch_num}/{total_batches} ({len(batch)} accounts) ---")
        for acc in batch:
            print(f"  @{acc['username']:<30} score={acc['score']}  reasons: {', '.join(acc['reasons'])}")

        choice = input(
            "\nRemove this batch? [y]es / [n]o, skip / [i]ndividually review / [q]uit: "
        ).strip().lower()

        if choice == "q":
            print("Stopping at your request.")
            break
        if choice == "n":
            continue

        for acc in batch:
            if shutdown_requested:
                print("\nStopping mid-batch (Ctrl+C). Progress has been logged.")
                return removed_count

            if choice == "i":
                sub = input(f"  Remove @{acc['username']}? [y/n/q]: ").strip().lower()
                if sub == "q":
                    return removed_count
                if sub != "y":
                    continue

            if dry_run:
                print(f"  [dry-run] would remove @{acc['username']}")
                log_line(f"DRY-RUN | {acc['username']} | pk={acc['pk']} | reasons={'; '.join(acc['reasons'])}")
                removed_count += 1
                continue

            try:
                ok = safe_api_call(cl.user_remove_follower, acc["pk"])
                if ok:
                    print(f"  [removed] @{acc['username']}")
                    log_line(f"REMOVED | {acc['username']} | pk={acc['pk']} | reasons={'; '.join(acc['reasons'])}")
                    removed_count += 1
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    print(f"  [!] Instagram refused to remove @{acc['username']}")
                    log_line(f"FAILED | {acc['username']} | pk={acc['pk']} | reason=api returned false")
            except KeyboardInterrupt:
                print("\nStopping (Ctrl+C). Progress has been logged.")
                return removed_count
            except Exception as e:
                consecutive_errors += 1
                print(f"  [!] Error removing @{acc['username']}: {e}")
                log_line(f"ERROR | {acc['username']} | pk={acc['pk']} | error={e}")

            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(
                    f"\n[ALERT] {MAX_CONSECUTIVE_ERRORS} errors in a row. "
                    "Stopping here so you can check your account before continuing."
                )
                return removed_count

            interruptible_sleep(random.uniform(*delay_range))

    return removed_count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Find and remove likely-bot Instagram followers.")
    parser.add_argument("--threshold", type=int, default=DEFAULT_BOT_THRESHOLD,
                         help=f"Number of bot signals required to flag an account (default: {DEFAULT_BOT_THRESHOLD})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                         help=f"How many accounts to review/remove per batch (default: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--delay-min", type=float, default=DEFAULT_DELAY_RANGE[0],
                         help="Minimum delay (seconds) between removals")
    parser.add_argument("--delay-max", type=float, default=DEFAULT_DELAY_RANGE[1],
                         help="Maximum delay (seconds) between removals")
    parser.add_argument("--new-account-days", type=int, default=DEFAULT_NEW_ACCOUNT_DAYS,
                         help=f"Accounts younger than this (estimated) count as 'very new' (default: {DEFAULT_NEW_ACCOUNT_DAYS})")
    parser.add_argument("--posts-to-check", type=int, default=DEFAULT_POSTS_TO_CHECK,
                         help=f"How many of your recent posts to scan for likes/comments (default: {DEFAULT_POSTS_TO_CHECK})")
    parser.add_argument("--dry-run", action="store_true",
                         help="Show what would be removed without actually removing anything")
    parser.add_argument("--reset-cache", action="store_true",
                         help="Ignore cached follower/engagement data and refetch everything")
    parser.add_argument("--anchor-username", type=str, default=None,
                         help="Only scan the followers that come after this account in Instagram's "
                              "follower ordering (most-recently-followed first). Use this when you "
                              "know roughly which follower marks the edge of a bot wave.")
    parser.add_argument("--anchor-window", type=int, default=6000,
                         help="How many followers after --anchor-username to scan (default: 6000)")
    return parser.parse_args()


def main():
    signal.signal(signal.SIGINT, handle_sigint)
    args = parse_args()

    if args.delay_min < 0 or args.delay_max < args.delay_min:
        print("Invalid delay range.")
        sys.exit(1)

    ensure_cache_dir()

    print("=== Instagram Bot Follower Remover ===")
    print("This tool only removes accounts you explicitly approve.\n")

    if args.reset_cache:
        for path in (FOLLOWERS_CACHE, WHITELIST_CACHE):
            if os.path.exists(path):
                os.remove(path)
        print("Cleared cached follower/engagement data.\n")

    cl = login()

    followers = fetch_followers(cl)
    if shutdown_requested:
        return

    if args.anchor_username:
        selected, anchor_pk = select_by_anchor(followers, args.anchor_username, args.anchor_window)
        if selected is None:
            print(f"\n[!] Could not find @{args.anchor_username} in your followers list. "
                  "Double-check the username and try again.")
            return
        print(
            f"\nFound @{args.anchor_username}. Restricting the scan to the "
            f"{len(selected)} follower(s) after them in Instagram's follower "
            f"ordering (most-recent-first - this ordering is observed behavior, "
            f"not guaranteed by Instagram)."
        )
        followers = selected

    details_cache = load_json(FOLLOWERS_CACHE, {})
    details_cache = fetch_follower_details(cl, followers, details_cache)
    if shutdown_requested:
        print("Run the script again to resume profile lookups.")
        return

    cached_whitelist = load_json(WHITELIST_CACHE, None)
    whitelist = build_interaction_whitelist(cl, args.posts_to_check, cached_whitelist)
    if shutdown_requested:
        return

    flagged, skipped_no_data, skipped_whitelisted = analyze(
        followers, details_cache, whitelist, args.threshold, args.new_account_days
    )

    print(f"\nAnalyzed {len(followers)} followers.")
    if skipped_whitelisted:
        print(f"  - {skipped_whitelisted} account(s) skipped: they liked/commented on your posts.")
    if skipped_no_data:
        print(f"  - {skipped_no_data} account(s) skipped: profile data unavailable.")

    print_flagged_summary(flagged)
    if not flagged:
        return

    proceed = input(
        f"\nReview these {len(flagged)} account(s) for removal in batches of "
        f"{args.batch_size}? [y/n]: "
    ).strip().lower()
    if proceed != "y":
        print("No accounts were removed.")
        return

    if args.dry_run:
        print("\n(Dry run mode: no follower will actually be removed.)")

    removed = remove_accounts(
        cl, flagged, args.batch_size, (args.delay_min, args.delay_max), args.dry_run
    )

    print(f"\nDone. {removed} account(s) {'would be ' if args.dry_run else ''}removed.")
    print(f"Details logged to {REMOVED_LOG}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
