# Flashcard Hub

A full-stack Flask web application that converts YouTube video transcripts into study flashcards, with authentication, streak tracking, daily quiz engagement, resource discovery, and AI helper integrations.

## Overview

Flashcard Hub is designed as a single-server app:

- Flask serves both frontend pages and backend APIs.
- Users authenticate via email/password, with Google sign-in support flow.
- Learning utilities are bundled into one dashboard:
  - YouTube-to-flashcard generation
  - Streak tracking
  - Daily mystery quiz
  - Resource locator/search
  - AI helper launch actions (ChatGPT/Gemini)

## Core Features

- **Authentication**
  - Signup/login/logout
  - Session token handling
  - Google auth start + callback flow (with fallback mode if OAuth credentials are missing)
- **Flashcard Generation**
  - Extracts transcript from YouTube
  - Generates cards with Gemini when available
  - Graceful fallback generation when AI provider is unavailable
- **Product Engagement**
  - Daily streak check-in and best streak tracking
  - Daily mystery quiz with reward points
- **Resource Discovery**
  - Auto recommendations based on generated flashcards
  - User-driven search endpoint for topic lookup (Google/YouTube)
- **UI/UX**
  - Dedicated login page and separate dashboard
  - Dark/Night theme toggle with persisted preference
  - Polished black/grey/white/cream visual style

## Project Structure

```text
Flashcard/
├── app.py          # Flask backend routes, auth/session, AI/resource logic
├── login.html      # Authentication page
├── index.html      # Main dashboard UI
├── .gitignore
└── README.md
```

## Tech Stack

- **Backend**: Python, Flask, Flask-CORS
- **Frontend**: Vanilla HTML/CSS/JS
- **AI**: Google Generative AI (Gemini), OpenAI API route scaffold
- **Data/Auth**: Supabase (with in-memory fallback paths)
- **Other**: YouTube transcript extraction, HTTP integrations via `requests`

## Local Setup

### 1) Clone

```bash
git clone https://github.com/hrs-bit/Flashcard.git
cd Flashcard
```

### 2) Install dependencies

Use Python launcher on Windows:

```powershell
py -m pip install flask flask-cors python-dotenv requests youtube-transcript-api google-generativeai supabase
```

### 3) Configure environment

Create `.env`:

```env
SUPABASE_URL="https://<your-project>.supabase.co"
SUPABASE_KEY="<your-supabase-key>"
GEMINI_API_KEY="<your-gemini-key>"
OPENAI_API_KEY="<your-openai-key>"
APP_SECRET="<any-random-secret>"
GOOGLE_REDIRECT_URL="http://127.0.0.1:5000/auth/google/callback"
GOOGLE_CLIENT_ID="<google-oauth-client-id>"
GOOGLE_CLIENT_SECRET="<google-oauth-client-secret>"
```

## Run

```powershell
py app.py
```

App URLs:

- Login: [http://127.0.0.1:5000/login](https://flashcard-he06.onrender.com)
- Dashboard: [http://127.0.0.1:5000/dashboard](https://flashcard-he06.onrender.com/dashboard)

## API Highlights

- `POST /auth/signup`
- `POST /auth/login`
- `POST /auth/logout`
- `GET /auth/me`
- `POST /auth/google/fallback-login`
- `POST /generate`
- `GET|POST /streak`
- `GET|POST /mystery-quiz`
- `GET /resources/recommend`
- `POST /resources/search`
- `POST /ai/gemini-chat`
- `POST /ai/chatgpt-chat`

## Deployment Notes

- Current code includes fallback behavior for environments where certain dependencies (or versions) are incompatible.
- For production:
  - Pin Python and dependency versions (recommended: Python 3.12)
  - Use a production WSGI server (e.g., gunicorn/waitress)
  - Configure proper OAuth credentials and HTTPS redirect URIs
  - Replace in-memory fallback persistence with full DB-backed flows

## Security Notes

- Do **not** commit `.env`.
- Rotate keys if they are accidentally exposed.
- Use least-privilege keys and server-side secrets only.

## Roadmap Suggestions

- Migrate from deprecated `google.generativeai` package to `google.genai`
- Add robust Supabase table migrations and row-level security policies
- Add test suite (API + UI smoke tests)
- Add pagination and relevance controls to resource search
- Add deploy pipeline (CI/CD + environment promotion)

---

Built for focused, gamified learning workflows with AI-assisted content generation.
