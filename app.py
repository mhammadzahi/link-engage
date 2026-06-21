import os
import json
import random
import re
import time
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

# Allow http://localhost redirect for local OAuth dev (oauthlib requires https otherwise)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
# LinkedIn returns scopes comma-separated and reordered; stop oauthlib raising on the "change"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

# ========================= CONFIG =========================
# A random topic is picked from this list each run (split into individual search terms).
TOPICS = [
    "الذكاء الاصطناعي", "تعلم الآلة", "الشبكات",
    "البرمجة", "تطوير البرمجيات", "الحوسبة السحابية", "أمن المعلومات", "تكنولوجيا المعلومات",
    "الإنترنت الأشياء", "البرمجيات مفتوحة المصدر"
]

NUM_POSTS = 8                # how many posts to pull from search
POSTS_PER_TOPIC = 3          # how many random posts to engage with before switching topic
TOPIC_INTERVAL = (900, 1800)  # seconds to wait between topics (15-30 min), randomized
TOPIC_INTERVAL = (240, 300) # seconds to wait between topics (4-5 min), randomized
DATE_FILTER = "past-24h"     # recency filter: "past-24h", "past-week", "past-month", or "" for any

# Reaction picker mapping (single keypress -> LinkedIn reaction name).
# REACTIONS = {"l": "Like", "c": "Celebrate", "s": "Support", "o": "Love", "i": "Insightful", "f": "Funny"}
REACTIONS = {"l": "Like", "c": "Celebrate", "s": "Support", "o": "Love", "i": "Insightful"}

SESSION_FILE = "linkedin_session.json"  # saved login; created on first run
headless_mode = False         # keep visible: safer, and you can watch / intervene

# Gemini: drafts a relevant comment per post. Keys are load-balanced via random.choice.
GEMINI_KEYS = [k for k in (os.getenv("gemini_api_key_1"), os.getenv("gemini_api_key_2"), os.getenv("gemini_api_key_3"), os.getenv("gemini_api_key_4")) if k]
GEMINI_MODEL = "gemini-2.5-flash-lite"  # cheapest current model

# Human-like pacing (seconds). Randomized so behavior isn't robotic.
DELAY_BETWEEN_POSTS = (4.75, 9.5)
DELAY_AFTER_TYPING = (2.85, 7.6)
SCROLL_PAUSE = (2.85, 6.65)
# =========================================================


def _sleep(rng):
    """Sleep a random duration within (lo, hi) to look less automated."""
    lo, hi = rng
    time.sleep(random.uniform(lo, hi))


_UI_NOISE = {
    "feed post", "follow", "like", "comment", "repost", "send", "more",
    "see more", "…more", "see translation", "subscribe",
    "reactions", "comments", "reposts", "activate to view larger image,",
}
# "12 reactions", "3 comments", "5 reposts", "1 comment"
_COUNT_LINE = re.compile(r"^\d[\d,\.]*\s*(reactions?|comments?|reposts?)$", re.I)
_ARABIC = re.compile(r"[؀-ۿ]")  # any Arabic character


def _clean_post_text(raw):
    """Strip LinkedIn UI chrome (Follow/Like/Comment/…, counts, '• 3rd+') so the post BODY
    dominates — otherwise English labels make Gemini reply in English on Arabic posts."""
    lines = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        if low in _UI_NOISE:
            continue
        if s.isdigit() or _COUNT_LINE.match(s):   # reaction / comment counts
            continue
        if s.startswith("•") or s.endswith("•"):  # "• 3rd+", "2h •"
            continue
        lines.append(s)
    # Drop the first 1-2 lines (author name + headline/connection degree), keep the body.
    body = lines[2:] if len(lines) > 3 else lines
    return "\n".join(body).strip() or raw.strip()


def _parse_gemini_json(raw):
    """Extract {"is_job": bool, "comment": str} from Gemini's reply (tolerates code fences)."""
    try:
        start, end = raw.find("{"), raw.rfind("}")
        obj = json.loads(raw[start:end + 1])
        return bool(obj.get("is_job", False)), (obj.get("comment") or "").strip()
    except Exception:
        # Couldn't parse JSON — treat the whole reply as a non-job comment so we don't lose it.
        return False, raw.strip()


def generate_comment(post_text, max_attempts=5):
    """Classify the post and draft a comment with Gemini.

    Returns (is_job, comment):
      - is_job=True  -> post is a job/hiring announcement; caller should NOT comment.
      - comment=str  -> the drafted comment (when not a job post).
      - (False, None) -> generation failed after retries; caller decides.

    Gemini's flash-lite endpoint often returns transient 503/429; we retry across keys."""
    if not GEMINI_KEYS:
        return False, None
    body = _clean_post_text(post_text)  # remove English UI chrome before language detection
    # Deterministic language rule: posts here are often bilingual (Arabic + English translation).
    # If any Arabic is present, force Arabic; otherwise match the post's language.
    if _ARABIC.search(body):
        lang_rule = "The comment MUST be written ENTIRELY in Arabic, even if the post also contains English."
    else:
        lang_rule = "Write the comment ENTIRELY in the same language as the post (English -> English). Never mix languages."
    prompt = (
        "You are given the body of a LinkedIn post. First decide if it is a job/hiring "
        "announcement or recruitment callout (e.g. 'we are hiring', a job listing, 'apply now', "
        "'open position'). Respond ONLY with a JSON object, no markdown, of the form:\n"
        '{"is_job": true|false, "comment": "<text>"}\n'
        "If is_job is true, set comment to an empty string.\n"
        f"Otherwise, write 'comment' reacting to the post. CRITICAL LANGUAGE RULE: {lang_rule}\n"
        "Make it a thoughtful, professional comment (1-2 sentences, under 280 chars) that adds "
        "real value: a concrete insight, relevant experience, or sharp point. Make a confident "
        "statement, NOT a question — no question mark. Avoid generic praise and filler. "
        "Sound like an expert practitioner. No hashtags, no emojis, no greetings.\n\n"
        f"POST:\n{body[:1500]}"
    )
    keys = random.sample(GEMINI_KEYS, len(GEMINI_KEYS))  # shuffle so load spreads across keys
    for attempt in range(max_attempts):
        key = keys[attempt % len(keys)]  # rotate keys across retries
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
        try:
            r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
            if r.status_code == 200:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if text:
                    is_job, comment = _parse_gemini_json(text)
                    return is_job, (comment or None)
            elif r.status_code in (429, 500, 503):
                wait = min(2 ** attempt, 8) + random.uniform(0, 1)  # backoff: ~1,2,4,8s + jitter
                print(f"  …Gemini {r.status_code} (overloaded), retry {attempt + 1}/{max_attempts} in {wait:.1f}s")
                time.sleep(wait)
                continue
            else:
                print(f"  ! Gemini {r.status_code}: {r.text[:120]}")
                return False, None
        except Exception as e:
            print(f"  ! Gemini request error ({e}), retry {attempt + 1}/{max_attempts}")
            time.sleep(1)
    print("  ! Gemini unavailable after retries.")
    return False, None


def ensure_logged_in(context, page):
    """Confirm the saved session is still authenticated; if not, let the user log in once."""
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    _sleep((2, 4))
    if "/login" in page.url or "/checkpoint" in page.url or "uas/login" in page.url:
        print("\nNot logged in. A browser window is open — log in to LinkedIn now.")
        print("Complete any 2FA / verification, land on your feed, then press Enter here.")
        input("Press Enter once you're on your LinkedIn feed... ")
        context.storage_state(path=SESSION_FILE)
        print(f"Session saved to {SESSION_FILE} — future runs won't ask you to log in.\n")
    else:
        print("Logged in via saved session.\n")


# LinkedIn ships fully hashed CSS classes that rotate every deploy, and no longer
# exposes data-urn / activity hrefs in search results. So we anchor ONLY on stable,
# accessibility-driven signals (aria-labels, ARIA roles, visible button text), which
# LinkedIn keeps consistent for screen-reader support.
def discover_posts(page, topic, num_posts):
    """Open content search for `topic`, scroll, and extract posts via accessibility anchors."""
    url = f"https://www.linkedin.com/search/results/content/?keywords={topic}&origin=SWITCH_SEARCH_VERTICAL"
    if DATE_FILTER:
        url += f"&datePosted=%22{DATE_FILTER}%22"  # %22 = the literal quotes LinkedIn requires
    print(f"Searching posts about '{topic}' (within {DATE_FILTER or 'any time'})...")
    page.goto(url, wait_until="domcontentloaded")
    _sleep((3, 5))

    # Scroll to load more results into the virtualized feed.
    for _ in range(max(3, num_posts // 2)):
        page.mouse.wheel(0, 2500)
        _sleep(SCROLL_PAUSE)

    # Each post has exactly one "Open control menu for post by {AUTHOR}" button — use it
    # as the per-post handle, and the nearest role=listitem ancestor as the container.
    menu_btns = page.get_by_role("button", name="Open control menu for post")
    count = menu_btns.count()
    print(f"Found {count} post(s) on page.\n")

    posts = []
    for i in range(min(count, num_posts)):
        btn = menu_btns.nth(i)
        try:
            label = btn.get_attribute("aria-label") or ""
            author = label.split(" by ", 1)[1].strip() if " by " in label else "Unknown author"
            container = btn.locator("xpath=ancestor::*[@role='listitem'][1]")
            if container.count() == 0:
                continue
            text = container.inner_text(timeout=2000).strip()
            posts.append({"author": author, "text": text[:500], "container": container})
        except Exception:
            continue

    return posts


def comment_on_post(page, post, text):
    """Comment on a post via the UI: open the composer, type, submit. All selectors are
    accessibility-based (role/label/text) — no hashed CSS classes."""
    c = post["container"]
    c.scroll_into_view_if_needed()
    _sleep((1, 2.5))

    # Open the comment composer (a <button> whose visible text is exactly "Comment").
    btn = c.get_by_role("button", name="Comment", exact=True).first
    if btn.count() == 0:
        print("  ! Could not find Comment button — skipping.")
        return False
    btn.click()
    _sleep((1.5, 3))

    # Composer is a TipTap contenteditable, role=textbox, labeled "Text editor for creating comment".
    editor = c.get_by_role("textbox", name="Text editor for creating comment").first
    if editor.count() == 0:
        editor = c.locator('[contenteditable="true"]').first  # fallback if the label changes
    if editor.count() == 0:
        print("  ! Comment editor didn't appear — skipping.")
        return False
    editor.click()
    editor.type(text, delay=random.uniform(40, 90))  # per-char delay = human-ish typing
    _sleep(DELAY_AFTER_TYPING)

    # The composer's post control is a button labeled "Comment" (some layouts use "Submit").
    # After opening the composer there are two "Comment" buttons — the action-bar toggle and
    # this submit; the submit is the last one. Prefer "Submit" if present, else the last "Comment".
    submit = c.get_by_role("button", name="Submit", exact=True).first
    if submit.count() == 0:
        comment_btns = c.get_by_role("button", name="Comment", exact=True)
        if comment_btns.count() < 2:
            print("  ! Could not find the composer's post button — skipping.")
            return False
        submit = comment_btns.last  # the composer submit, not the action-bar toggle
    submit.click()
    _sleep((2, 4))
    print("  ✓ Comment posted.")
    return True


def expand_text(post):
    """Click the post's 'see more' expander (if present) and return the full text."""
    c = post["container"]
    for name in ("see more", "…more", "more"):
        btn = c.get_by_role("button", name=name, exact=True).first
        try:
            if btn.count() > 0:
                btn.click()
                _sleep((0.5, 1.5))
                break
        except Exception:
            continue
    try:
        return c.inner_text(timeout=2000).strip()
    except Exception:
        return post["text"]


def react_to_post(page, post, reaction):
    """Apply a reaction. 'Like' clicks the main button; others open the hover flyout."""
    c = post["container"]
    like = c.get_by_role("button", name="Reaction button state: no reaction").first
    if like.count() == 0:
        print("  ! Reaction button not found (maybe already reacted) — skipping reaction.")
        return False
    like.scroll_into_view_if_needed()
    _sleep((1, 2.5))

    applied = reaction
    if reaction == "Like":
        like.click()
    else:
        like.hover()  # reveals the Celebrate/Support/Love/Insightful/Funny flyout
        _sleep((1.5, 3))
        opt = page.get_by_role("button", name=reaction, exact=True).first
        if opt.count() == 0:
            print(f"  ! '{reaction}' not in flyout — defaulting to Like.")
            like.click()
            applied = "Like"  # report what actually happened
        else:
            opt.click()
    _sleep((1, 2.5))
    print(f"  ✓ Reacted: {applied}")
    return True


def review_and_engage(page, posts):
    """Present posts in random order; for each, you approve a reaction and/or a comment.
    Approval stays in the loop by design — nothing is posted without your keypress.
    Returns "quit" if the user asked to stop the whole run."""
    posted = 0
    for idx, post in enumerate(posts, 1):
        # After engaging a post the feed re-renders/scrolls, which can detach a later
        # post's locator. Fail fast (don't wait the default 30s) and skip if unreachable.
        try:
            post["container"].scroll_into_view_if_needed(timeout=8000)
        except Exception:
            print(f"[{idx}/{len(posts)}] {post['author']}: post no longer reachable — skipping.")
            continue
        _sleep((1, 3))
        full_text = expand_text(post)  # read the whole post before deciding

        print("=" * 70)
        print(f"[{idx}/{len(posts)}]  {post['author']}")
        print(full_text[:600])
        print("-" * 70)

        # --- Reaction (optional) ---
        # rx = input("React? (l)ike (c)elebrate (s)upport l(o)ve (i)nsightful (f)unny / Enter=skip / (q)uit: ").strip().lower()
        rx = random.choice(list(REACTIONS.keys()) + [""])  # Randomly choose a reaction
        if rx == "q":
            return "quit"
        if rx in REACTIONS:
            react_to_post(page, post, REACTIONS[rx])
            _sleep(DELAY_AFTER_TYPING)

        # --- Comment (optional) ---
        # if input("Comment on this post? (y/N): ").strip().lower() == "y":
        if random.choice([True, True, True, False]) == True:  # 75% chance
            print("  Generating comment with Gemini...")
            is_job, suggestion = generate_comment(full_text)
            if is_job:
                print("  Job/hiring post — skipping comment.")
            elif suggestion:
                print(f"\nGemini draft: {suggestion}")
                if comment_on_post(page, post, suggestion):
                    posted += 1
            else:
                print("  No comment generated — skipping.")

        _sleep(DELAY_BETWEEN_POSTS)

    print(f"\nEngaged this topic. Commented on {posted} post(s).")
    return "done"


# ===================== MAIN =====================
if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless_mode)
        context = (
            browser.new_context(storage_state=SESSION_FILE)
            if os.path.exists(SESSION_FILE)
            else browser.new_context()
        )
        page = context.new_page()

        ensure_logged_in(context, page)

        # Infinite topic-rotation loop: pick a topic, engage a couple of random posts
        # (each gated by your approval), wait 5-10 min, then move to a new topic.
        # Press 'q' at any post prompt, or Ctrl+C, to stop.
        try:
            while True:
                topic = random.choice(TOPICS)
                print("\n" + "#" * 70)
                print(f"# Topic this round: {topic}")
                print("#" * 70 + "\n")

                try:
                    posts = discover_posts(page, topic, NUM_POSTS)
                except Exception as e:
                    print(f"Discovery failed ({e}); moving on.")
                    posts = []

                if not posts:
                    print("No posts extracted (markup change or empty results) — trying another topic.")
                else:
                    selected = random.sample(posts, min(POSTS_PER_TOPIC, len(posts)))
                    print(f"Engaging {len(selected)} random post(s) for '{topic}'.\n")
                    if review_and_engage(page, selected) == "quit":
                        print("Stopping at your request.")
                        break

                wait = random.uniform(*TOPIC_INTERVAL)
                print(f"\nWaiting {wait/60:.1f} min before next topic... (Ctrl+C to stop)")
                time.sleep(wait)
        except KeyboardInterrupt:
            print("\nInterrupted — shutting down.")

        browser.close()


# ---------------------------------------------------------------------------
# OAuth helpers (NOT used by the Playwright flow above — kept for reference).
# The w_member_social token can comment via the official API, but only when you
# already have a post's activity URN; it cannot search/read others' feeds.
# ---------------------------------------------------------------------------
def get_authorization_url():
    from requests_oauthlib import OAuth2Session
    client_id = os.getenv("CLIENT_ID")
    redirect_uri = "http://localhost:8080/callback"
    scope = ["openid", "profile", "email", "w_member_social"]
    linkedin = OAuth2Session(client_id, redirect_uri=redirect_uri, scope=scope)
    auth_url, state = linkedin.authorization_url("https://www.linkedin.com/oauth/v2/authorization")
    print(f"Open this URL in browser:\n{auth_url}\n")
    return linkedin, state


def get_access_token(linkedin, auth_response):
    token = linkedin.fetch_token(
        "https://www.linkedin.com/oauth/v2/accessToken",
        client_secret=os.getenv("CLIENT_SECRET"),
        include_client_id=True,  # LinkedIn wants client_id + secret in the POST body
        authorization_response=auth_response,
    )
    return token["access_token"]
