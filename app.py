import os
import random
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
    "Backend Development", "Cloud Computing", "Python", "Java", "AI",
    "FastAPI", "Django", "Spring Boot", "PostgreSQL", "AWS",
    "IoT", "IIoT", "Linux", "RedHat", "Fedora", "Open Source", "Kubernetes", "Docker", "DevOps", "CI/CD",
    "تقنية المعلومات", "الذكاء الاصطناعي", "البرمجة", "تطوير البرمجيات", "الحوسبة السحابية", "أمن المعلومات",
]

NUM_POSTS = 8                # how many posts to pull from search
POSTS_PER_TOPIC = 2          # how many random posts to engage with before switching topic
TOPIC_INTERVAL = (300, 600)  # seconds to wait between topics (5-10 min), randomized

# Reaction picker mapping (single keypress -> LinkedIn reaction name).
# REACTIONS = {"l": "Like", "c": "Celebrate", "s": "Support", "o": "Love", "i": "Insightful", "f": "Funny"}
REACTIONS = {"l": "Like", "c": "Celebrate", "s": "Support", "o": "Love", "i": "Insightful"}

SESSION_FILE = "linkedin_session.json"  # saved login; created on first run
headless_mode = True         # keep visible: safer, and you can watch / intervene

# Gemini: drafts a relevant comment per post. Keys are load-balanced via random.choice.
GEMINI_KEYS = [k for k in (os.getenv("gemini_api_key_1"), os.getenv("gemini_api_key_2"), os.getenv("gemini_api_key_3"), os.getenv("gemini_api_key_4")) if k]
GEMINI_MODEL = "gemini-2.5-flash-lite"  # cheapest current model

# Human-like pacing (seconds). Randomized so behavior isn't robotic.
DELAY_BETWEEN_POSTS = (15, 30)
DELAY_AFTER_TYPING = (1.5, 4.0)
SCROLL_PAUSE = (1.5, 3.5)
# =========================================================


def _sleep(rng):
    """Sleep a random duration within (lo, hi) to look less automated."""
    lo, hi = rng
    time.sleep(random.uniform(lo, hi))


def generate_comment(post_text, max_attempts=5):
    """Draft a short, relevant LinkedIn comment for `post_text` using Gemini.

    Gemini's flash-lite endpoint often returns transient 503/429 (overloaded / rate-limit).
    We retry, rotating across keys with exponential backoff, so a blip doesn't kill the draft.
    Returns the generated text, or None if every attempt failed (caller decides what to do)."""
    if not GEMINI_KEYS:
        return None
    prompt = (
        "Write a thoughtful, professional LinkedIn comment (1-2 sentences, under 280 chars) "
        "reacting to the post below. Write the comment in the SAME language as the post "
        "(e.g. if the post is in Arabic, reply in Arabic). "
        "Add real value: share a concrete insight, a relevant experience, or a sharp point "
        "that extends the discussion. Make a confident statement, NOT a question — "
        "do not end with a question mark. Avoid generic praise and filler. "
        "Sound like an expert practitioner. No hashtags, no emojis, no greetings, "
        "just the comment text.\n\n"
        f"POST:\n{post_text[:1500]}"
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
                    return text
            elif r.status_code in (429, 500, 503):
                wait = min(2 ** attempt, 8) + random.uniform(0, 1)  # backoff: ~1,2,4,8s + jitter
                print(f"  …Gemini {r.status_code} (overloaded), retry {attempt + 1}/{max_attempts} in {wait:.1f}s")
                time.sleep(wait)
                continue
            else:
                print(f"  ! Gemini {r.status_code}: {r.text[:120]}")
                return None
        except Exception as e:
            print(f"  ! Gemini request error ({e}), retry {attempt + 1}/{max_attempts}")
            time.sleep(1)
    print("  ! Gemini unavailable after retries.")
    return None


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
    print(f"Searching posts about '{topic}'...")
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

    if reaction == "Like":
        like.click()
    else:
        like.hover()  # reveals the Celebrate/Support/Love/Insightful/Funny flyout
        _sleep((1.5, 3))
        opt = page.get_by_role("button", name=reaction, exact=True).first
        if opt.count() == 0:
            print(f"  ! '{reaction}' not in flyout — defaulting to Like.")
            like.click()
        else:
            opt.click()
    _sleep((1, 2.5))
    print(f"  ✓ Reacted: {reaction}")
    return True


def review_and_engage(page, posts):
    """Present posts in random order; for each, you approve a reaction and/or a comment.
    Approval stays in the loop by design — nothing is posted without your keypress.
    Returns "quit" if the user asked to stop the whole run."""
    posted = 0
    for idx, post in enumerate(posts, 1):
        post["container"].scroll_into_view_if_needed()
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
            suggestion = generate_comment(full_text)
            if suggestion:
                print(f"\nGemini draft: {suggestion}")
                # draft = input("Edit, or press Enter to use it: ").strip() or suggestion
                draft = suggestion
            else:
                draft = input("Gemini failed. Type a comment (or Enter to skip): ").strip()

            if draft and comment_on_post(page, post, draft):
                posted += 1

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
