import hashlib
import json
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

import google.generativeai as genai
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi

try:
    from supabase import create_client
    SUPABASE_IMPORT_ERROR = None
except Exception as supabase_error:
    create_client = None
    SUPABASE_IMPORT_ERROR = str(supabase_error)

load_dotenv()

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
APP_SECRET = os.environ.get("APP_SECRET", "flashcard-dev-secret")
GOOGLE_REDIRECT_URL = os.environ.get("GOOGLE_REDIRECT_URL", "http://127.0.0.1:5000/auth/google/callback")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

MEM_USERS = {}
MEM_SESSIONS = {}
MEM_STREAKS = {}
MEM_DECKS = {}
MEM_RESOURCES = {}
MEM_QUIZ = {}
MEM_GOOGLE_STATES = {}

supabase = None
if create_client and SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Supabase client init failed: {e}")
elif SUPABASE_IMPORT_ERROR:
    print(f"Supabase import skipped: {SUPABASE_IMPORT_ERROR}")

model = None
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")
else:
    print("GEMINI_API_KEY missing. Gemini endpoints will return clear errors.")


def _hash_text(text):
    return hashlib.sha256((text + APP_SECRET).encode("utf-8")).hexdigest()


def _make_local_token(email):
    now_key = datetime.utcnow().isoformat()
    return _hash_text(f"{email}:{now_key}")


def _error(message, status=400, hint=None):
    payload = {"status": "error", "message": message}
    if hint:
        payload["hint"] = hint
    return jsonify(payload), status


def extract_video_id(youtube_url):
    parsed = urlparse(youtube_url)
    host = parsed.netloc.lower()
    if "youtube.com" in host:
        return parse_qs(parsed.query).get("v", [None])[0]
    if "youtu.be" in host:
        return parsed.path.lstrip("/") or None
    return None


def build_fallback_cards(video_text, count=5):
    sentences = [s.strip() for s in video_text.replace("\n", " ").split(".") if s.strip()]
    cards = []
    for i, sentence in enumerate(sentences[:count]):
        cards.append(
            {
                "question": f"What is a key point #{i + 1} from this video?",
                "answer": sentence,
            }
        )
    while len(cards) < count:
        cards.append(
            {
                "question": f"What is a key point #{len(cards) + 1} from this video?",
                "answer": "Transcript did not contain enough clear sentences for more cards.",
            }
        )
    return cards


def transcript_text_from_video(video_id):
    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join([t["text"] for t in transcript_list])
    transcript = YouTubeTranscriptApi().fetch(video_id)
    return " ".join([snippet.text for snippet in transcript])


def generate_cards_from_text(video_text):
    if not model:
        return build_fallback_cards(video_text, count=5)
    try:
        prompt = f"""
        Read this transcript and create 5 educational flashcards.
        Return ONLY a raw JSON array. No markdown, no formatting, no extra words.
        Example format: [{{"question": "Q1", "answer": "A1"}}, {{"question": "Q2", "answer": "A2"}}]

        Transcript: {video_text[:5000]}
        """
        response = model.generate_content(prompt)
        clean_json = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_json)
    except Exception as model_error:
        print(f"Gemini generation failed, using fallback cards: {model_error}")
        return build_fallback_cards(video_text, count=5)


def _supabase_signup(email, password):
    resp = supabase.auth.sign_up({"email": email, "password": password})
    user_obj = getattr(resp, "user", None)
    session_obj = getattr(resp, "session", None)
    if not user_obj:
        return None
    return {
        "user_id": user_obj.id,
        "email": user_obj.email,
        "access_token": getattr(session_obj, "access_token", None),
    }


def _supabase_login(email, password):
    resp = supabase.auth.sign_in_with_password({"email": email, "password": password})
    user_obj = getattr(resp, "user", None)
    session_obj = getattr(resp, "session", None)
    if not user_obj or not session_obj:
        return None
    return {
        "user_id": user_obj.id,
        "email": user_obj.email,
        "access_token": session_obj.access_token,
    }


def _local_signup(email, password):
    if email in MEM_USERS:
        return None
    user_id = _hash_text(email)[:16]
    MEM_USERS[email] = {"user_id": user_id, "password_hash": _hash_text(password)}
    token = _make_local_token(email)
    MEM_SESSIONS[token] = {"user_id": user_id, "email": email}
    return {"user_id": user_id, "email": email, "access_token": token}


def _local_login(email, password):
    user = MEM_USERS.get(email)
    if not user or user["password_hash"] != _hash_text(password):
        return None
    token = _make_local_token(email)
    MEM_SESSIONS[token] = {"user_id": user["user_id"], "email": email}
    return {"user_id": user["user_id"], "email": email, "access_token": token}


def _local_login_or_create(email):
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    if normalized not in MEM_USERS:
        MEM_USERS[normalized] = {
            "user_id": _hash_text(normalized)[:16],
            "password_hash": _hash_text("google-oauth-user"),
        }
    token = _make_local_token(normalized)
    MEM_SESSIONS[token] = {"user_id": MEM_USERS[normalized]["user_id"], "email": normalized}
    return {"user_id": MEM_USERS[normalized]["user_id"], "email": normalized, "access_token": token}


def _resolve_user_from_token(token):
    if not token:
        return None
    if supabase:
        try:
            resp = supabase.auth.get_user(token)
            user_obj = getattr(resp, "user", None)
            if user_obj:
                return {"user_id": user_obj.id, "email": user_obj.email, "token": token}
        except Exception as e:
            print(f"Supabase token validation failed: {e}")
    session = MEM_SESSIONS.get(token)
    if session:
        return {"user_id": session["user_id"], "email": session["email"], "token": token}
    return None


def auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _error("Missing Bearer token.", 401, hint="Login first.")
        token = auth_header.replace("Bearer ", "", 1).strip()
        user_ctx = _resolve_user_from_token(token)
        if not user_ctx:
            return _error("Invalid or expired session.", 401, hint="Please login again.")
        request.user_ctx = user_ctx
        return fn(*args, **kwargs)

    return wrapper


def _resource_recommendations(cards):
    words = []
    for card in cards:
        text = f"{card.get('question', '')} {card.get('answer', '')}"
        for raw in text.split():
            w = raw.strip(".,:;!?()[]{}\"'").lower()
            if len(w) > 5 and w.isalpha():
                words.append(w)
    unique = []
    for w in words:
        if w not in unique:
            unique.append(w)
    top = unique[:5] or ["learning", "study"]
    resources = []
    for topic in top:
        resources.append(
            {
                "topic": topic,
                "title": f"Learn more about {topic}",
                "url": f"https://www.google.com/search?q={quote_plus(topic + ' tutorial')}",
                "source": "google_search",
            }
        )
        resources.append(
            {
                "topic": topic,
                "title": f"{topic.title()} on Wikipedia",
                "url": f"https://en.wikipedia.org/wiki/{quote_plus(topic)}",
                "source": "wikipedia",
            }
        )
    return resources[:8]


def _today_iso():
    return date.today().isoformat()


def _parse_views_to_int(view_text):
    if not view_text:
        return 0
    cleaned = view_text.lower().replace("views", "").strip().replace(",", "")
    multiplier = 1
    if cleaned.endswith("k"):
        multiplier = 1000
        cleaned = cleaned[:-1]
    elif cleaned.endswith("m"):
        multiplier = 1000000
        cleaned = cleaned[:-1]
    elif cleaned.endswith("b"):
        multiplier = 1000000000
        cleaned = cleaned[:-1]
    try:
        return int(float(cleaned) * multiplier)
    except Exception:
        return 0


def _search_youtube_ranked(query, limit=8):
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    resp = requests.get(search_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    html = resp.text
    ranked = []

    # Parse key metadata from embedded JSON chunks.
    video_ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
    titles = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"\}\]\}', html)
    views = re.findall(r'"viewCountText":\{"simpleText":"([^"]+)"\}', html)
    channels = re.findall(r'"ownerText":\{"runs":\[\{"text":"([^"]+)"', html)

    for i, vid in enumerate(video_ids[: max(limit * 2, 20)]):
        title = titles[i] if i < len(titles) else f"YouTube video {i + 1}"
        views_text = views[i] if i < len(views) else "N/A"
        channel = channels[i] if i < len(channels) else "YouTube"
        views_num = _parse_views_to_int(views_text)
        ranked.append(
            {
                "platform": "youtube",
                "title": title,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "channel": channel,
                "views": views_text,
                "likes": "N/A",
                "score": views_num,
            }
        )

    # Remove duplicates by URL.
    unique = {}
    for r in ranked:
        if r["url"] not in unique:
            unique[r["url"]] = r
    ranked = list(unique.values())
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:limit]


def _search_google_ranked(query, limit=8):
    url = "https://api.duckduckgo.com/"
    resp = requests.get(
        url,
        params={"q": query, "format": "json", "no_redirect": 1, "no_html": 1},
        timeout=20,
    )
    payload = resp.json()
    out = []
    for topic in payload.get("RelatedTopics", []):
        if isinstance(topic, dict) and topic.get("FirstURL") and topic.get("Text"):
            out.append(
                {
                    "platform": "google",
                    "title": topic.get("Text"),
                    "url": topic.get("FirstURL"),
                    "channel": "Web",
                    "views": "N/A",
                    "likes": "N/A",
                    "score": 1,
                }
            )
            if len(out) >= limit:
                break
        for nested in topic.get("Topics", []) if isinstance(topic, dict) else []:
            if nested.get("FirstURL") and nested.get("Text"):
                out.append(
                    {
                        "platform": "google",
                        "title": nested.get("Text"),
                        "url": nested.get("FirstURL"),
                        "channel": "Web",
                        "views": "N/A",
                        "likes": "N/A",
                        "score": 1,
                    }
                )
                if len(out) >= limit:
                    break
        if len(out) >= limit:
            break
    if out:
        return out[:limit]

    # Fallback parser for HTML results when instant answers are empty.
    html_resp = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    html_text = html_resp.text
    links = re.findall(r'<a rel="nofollow" class="result__a" href="(.*?)">(.*?)</a>', html_text)
    for href, title_html in links[:limit]:
        title = re.sub(r"<.*?>", "", title_html)
        out.append(
            {
                "platform": "google",
                "title": title,
                "url": href,
                "channel": "Web",
                "views": "N/A",
                "likes": "N/A",
                "score": 1,
            }
        )
    if out:
        return out[:limit]

    # Final fallback with reliable query links when search providers block scraping.
    safe_query = quote_plus(query)
    return [
        {
            "platform": "google",
            "title": f"Top Google results for {query}",
            "url": f"https://www.google.com/search?q={safe_query}",
            "channel": "Google",
            "views": "N/A",
            "likes": "N/A",
            "score": 1,
        },
        {
            "platform": "google",
            "title": f"YouTube results for {query}",
            "url": f"https://www.youtube.com/results?search_query={safe_query}",
            "channel": "YouTube",
            "views": "N/A",
            "likes": "N/A",
            "score": 1,
        },
        {
            "platform": "google",
            "title": f"Wikipedia resources for {query}",
            "url": f"https://en.wikipedia.org/w/index.php?search={safe_query}",
            "channel": "Wikipedia",
            "views": "N/A",
            "likes": "N/A",
            "score": 1,
        },
    ]


@app.route("/")
def home():
    return send_file("login.html")


@app.route("/login")
def login_page():
    return send_file("login.html")


@app.route("/dashboard")
def dashboard_page():
    return send_file("index.html")


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "success",
            "supabase_connected": bool(supabase),
            "gemini_ready": bool(GEMINI_API_KEY),
            "openai_ready": bool(OPENAI_API_KEY),
        }
    )


@app.route("/auth/signup", methods=["POST"])
def signup():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not email or not password:
        return _error("Email and password are required.", 400)
    if len(password) < 6:
        return _error("Password must be at least 6 characters.", 400)
    try:
        session_data = _supabase_signup(email, password) if supabase else _local_signup(email, password)
        if not session_data:
            return _error("Signup failed or user already exists.", 400)
        return jsonify({"status": "success", "session": session_data})
    except Exception as e:
        print(f"Signup error: {e}")
        return _error("Signup failed.", 500, hint=str(e))


@app.route("/auth/login", methods=["POST"])
def login():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not email or not password:
        return _error("Email and password are required.", 400)
    try:
        session_data = _supabase_login(email, password) if supabase else _local_login(email, password)
        if not session_data:
            return _error("Invalid credentials.", 401)
        return jsonify({"status": "success", "session": session_data})
    except Exception as e:
        print(f"Login error: {e}")
        return _error("Login failed.", 500, hint=str(e))


@app.route("/auth/google/start")
def auth_google_start():
    # Real Google OAuth when credentials are configured.
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        state = secrets.token_urlsafe(24)
        MEM_GOOGLE_STATES[state] = int(time.time()) + 600
        query = urlencode(
            {
                "client_id": GOOGLE_CLIENT_ID,
                "redirect_uri": GOOGLE_REDIRECT_URL,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "prompt": "select_account",
                "access_type": "offline",
            }
        )
        url = f"https://accounts.google.com/o/oauth2/v2/auth?{query}"
        return jsonify({"status": "success", "url": url})

    # Fallback path: local Google-like login if OAuth credentials are missing.
    url = "http://127.0.0.1:5000/auth/google/fallback"
    return jsonify({"status": "success", "url": url})


@app.route("/auth/google/callback")
def auth_google_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return (
            "<html><body style='font-family:Arial;padding:20px;'>"
            "<h3>Google sign-in failed.</h3><p>Missing callback parameters.</p>"
            "</body></html>"
        )
    expires_at = MEM_GOOGLE_STATES.pop(state, 0)
    if int(time.time()) > int(expires_at):
        return (
            "<html><body style='font-family:Arial;padding:20px;'>"
            "<h3>Google sign-in failed.</h3><p>Session expired. Try again.</p>"
            "</body></html>"
        )
    try:
        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URL,
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        token_payload = token_resp.json()
        id_token = token_payload.get("id_token")
        if not id_token:
            raise ValueError(f"No id_token in response: {token_payload}")
        info_resp = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": id_token},
            timeout=20,
        )
        info = info_resp.json()
        email = (info.get("email") or "").strip().lower()
        session = _local_login_or_create(email)
        if not session:
            raise ValueError("Failed creating local session for Google user.")
        token = session["access_token"]
        return f"""
        <html><body style='font-family:Arial;padding:20px;'>
        <h3>Google sign-in successful.</h3>
        <p>You can close this window now.</p>
        <script>
        window.opener && window.opener.postMessage({{type:"google_auth_success", token:"{token}"}}, window.location.origin);
        setTimeout(function(){{ window.close(); }}, 600);
        </script>
        </body></html>
        """
    except Exception as e:
        return (
            "<html><body style='font-family:Arial;padding:20px;'>"
            "<h3>Google sign-in failed.</h3>"
            f"<p>{str(e)}</p>"
            "</body></html>"
        )


@app.route("/auth/google/fallback")
def auth_google_fallback():
    return """
    <html>
    <body style="font-family:Arial;padding:20px;background:#111;color:#fff;">
      <h3>Google login fallback</h3>
      <p>OAuth keys are not configured, so use your Gmail to continue.</p>
      <input id="email" placeholder="you@gmail.com" style="padding:10px;border-radius:8px;border:1px solid #666;width:100%;max-width:360px;" />
      <br/><br/>
      <button onclick="go()" style="padding:10px 14px;border-radius:8px;border:none;background:#f5eee2;color:#111;cursor:pointer;">Continue</button>
      <p id="msg"></p>
      <script>
        async function go() {
          const email = document.getElementById("email").value.trim();
          const res = await fetch("/auth/google/fallback-login", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({email})
          });
          const data = await res.json();
          if (!res.ok || data.status === "error") {
            document.getElementById("msg").textContent = data.message || "Failed";
            return;
          }
          window.opener && window.opener.postMessage({type:"google_auth_success", token:data.session.access_token}, window.location.origin);
          setTimeout(() => window.close(), 400);
        }
      </script>
    </body>
    </html>
    """


@app.route("/auth/google/fallback-login", methods=["POST"])
def auth_google_fallback_login():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return _error("Please provide a valid email.", 400)
    if not email.endswith("@gmail.com"):
        return _error("Please use a Gmail address for Google login fallback.", 400)
    session = _local_login_or_create(email)
    if not session:
        return _error("Failed to create Google fallback session.", 500)
    return jsonify({"status": "success", "session": session})


@app.route("/auth/logout", methods=["POST"])
@auth_required
def logout():
    token = request.user_ctx["token"]
    MEM_SESSIONS.pop(token, None)
    return jsonify({"status": "success", "message": "Logged out."})


@app.route("/auth/me")
@auth_required
def auth_me():
    return jsonify({"status": "success", "user": request.user_ctx})


@app.route("/generate", methods=["POST"])
@auth_required
def generate_cards():
    try:
        data = request.json or {}
        youtube_url = (data.get("url") or "").strip()
        if not youtube_url:
            return _error("Please provide a YouTube URL.", 400)
        video_id = extract_video_id(youtube_url)
        if not video_id:
            return _error("Invalid YouTube URL format.", 400)

        video_text = transcript_text_from_video(video_id)
        flashcards_data = generate_cards_from_text(video_text)
        user_id = request.user_ctx["user_id"]
        deck_record = {
            "user_id": user_id,
            "source_url": youtube_url,
            "flashcards": flashcards_data,
            "created_at": datetime.utcnow().isoformat(),
        }

        MEM_DECKS[user_id] = deck_record
        if supabase:
            try:
                supabase.table("user_decks").insert(deck_record).execute()
            except Exception as db_error:
                print(f"Saving user_decks failed: {db_error}")

        return jsonify({"status": "success", "cards": flashcards_data}), 200
    except Exception as e:
        print(f"Generate error: {e}")
        return _error("Generation failed.", 500, hint=str(e))


@app.route("/streak", methods=["GET", "POST"])
@auth_required
def streak():
    user_id = request.user_ctx["user_id"]
    if user_id not in MEM_STREAKS:
        MEM_STREAKS[user_id] = {
            "current_streak": 0,
            "best_streak": 0,
            "last_active_date": None,
            "updated_at": datetime.utcnow().isoformat(),
        }
    state = MEM_STREAKS[user_id]

    if request.method == "POST":
        today = date.today()
        last = state["last_active_date"]
        if last == today.isoformat():
            return jsonify({"status": "success", "streak": state, "message": "Already checked in today."})
        if last == (today - timedelta(days=1)).isoformat():
            state["current_streak"] += 1
        else:
            state["current_streak"] = 1
        state["best_streak"] = max(state["best_streak"], state["current_streak"])
        state["last_active_date"] = today.isoformat()
        state["updated_at"] = datetime.utcnow().isoformat()

    if supabase:
        try:
            row = {"user_id": user_id, **state}
            supabase.table("user_streaks").upsert(row).execute()
        except Exception as db_error:
            print(f"Saving user_streaks failed: {db_error}")

    return jsonify({"status": "success", "streak": state})


@app.route("/resources/recommend", methods=["GET"])
@auth_required
def resources_recommend():
    user_id = request.user_ctx["user_id"]
    last_deck = MEM_DECKS.get(user_id)
    cards = (last_deck or {}).get("flashcards", [])
    resources = _resource_recommendations(cards)
    MEM_RESOURCES[user_id] = resources
    if supabase and resources:
        try:
            rows = [{"user_id": user_id, **r, "created_at": datetime.utcnow().isoformat()} for r in resources]
            supabase.table("user_resources").insert(rows).execute()
        except Exception as db_error:
            print(f"Saving user_resources failed: {db_error}")
    return jsonify({"status": "success", "resources": resources})


@app.route("/resources/search", methods=["POST"])
@auth_required
def resources_search():
    data = request.json or {}
    query = (data.get("query") or "").strip()
    platform = (data.get("platform") or "youtube").strip().lower()
    if not query:
        return _error("Query is required.", 400)
    if platform not in {"youtube", "google"}:
        return _error("Platform must be 'youtube' or 'google'.", 400)
    try:
        if platform == "youtube":
            results = _search_youtube_ranked(query, limit=8)
        else:
            results = _search_google_ranked(query, limit=8)
        return jsonify({"status": "success", "platform": platform, "query": query, "results": results})
    except Exception as e:
        return _error("Resource search failed.", 500, hint=str(e))


@app.route("/mystery-quiz", methods=["GET", "POST"])
@auth_required
def mystery_quiz():
    user_id = request.user_ctx["user_id"]
    today = _today_iso()

    if request.method == "GET":
        current = MEM_QUIZ.get(user_id)
        if not current or current["quiz_date"] != today:
            deck = MEM_DECKS.get(user_id, {})
            cards = deck.get("flashcards") or build_fallback_cards("Daily mystery fallback question.")
            source = cards[0]
            current = {
                "quiz_date": today,
                "question": source["question"],
                "answer": source["answer"],
                "completed": False,
                "reward_points": 15,
            }
            MEM_QUIZ[user_id] = current
        return jsonify({"status": "success", "quiz": current})

    data = request.json or {}
    submitted_answer = (data.get("answer") or "").strip().lower()
    quiz = MEM_QUIZ.get(user_id)
    if not quiz or quiz["quiz_date"] != today:
        return _error("No active quiz for today.", 400, hint="Call GET /mystery-quiz first.")
    if quiz["completed"]:
        return jsonify({"status": "success", "quiz": quiz, "message": "Quiz already completed today."})

    correct = quiz["answer"].strip().lower()
    is_correct = correct in submitted_answer or submitted_answer in correct if submitted_answer else False
    quiz["completed"] = True
    if not is_correct:
        quiz["reward_points"] = max(5, quiz["reward_points"] // 2)

    if supabase:
        try:
            row = {"user_id": user_id, **quiz}
            supabase.table("daily_mystery_quiz").upsert(row).execute()
        except Exception as db_error:
            print(f"Saving daily_mystery_quiz failed: {db_error}")

    return jsonify(
        {
            "status": "success",
            "quiz": quiz,
            "is_correct": is_correct,
            "message": "Mystery quiz completed.",
        }
    )


@app.route("/ai/gemini-chat", methods=["POST"])
@auth_required
def ai_gemini_chat():
    data = request.json or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return _error("Prompt is required.", 400)
    if not model:
        return _error("Gemini is not configured.", 400, hint="Add GEMINI_API_KEY in .env")
    try:
        reply = model.generate_content(prompt)
        return jsonify({"status": "success", "reply": reply.text})
    except Exception as e:
        return _error("Gemini request failed.", 500, hint=str(e))


@app.route("/ai/chatgpt-chat", methods=["POST"])
@auth_required
def ai_chatgpt_chat():
    data = request.json or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return _error("Prompt is required.", 400)
    if not OPENAI_API_KEY:
        return _error("ChatGPT is not configured.", 400, hint="Add OPENAI_API_KEY in .env")
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
            },
            timeout=30,
        )
        payload = resp.json()
        if resp.status_code >= 400:
            return _error("ChatGPT request failed.", resp.status_code, hint=str(payload))
        message = payload["choices"][0]["message"]["content"]
        return jsonify({"status": "success", "reply": message})
    except Exception as e:
        return _error("ChatGPT request failed.", 500, hint=str(e))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)