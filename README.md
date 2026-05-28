# Neuorise Healing Music Generator

A production-shaped full-stack app for adaptive therapeutic music generation.

## Features

- SQLite storage for users, auth sessions, surveys, generated tracks, and feedback
- Server-side account creation and login with PBKDF2 password hashing
- HttpOnly cookie sessions
- Protected survey, generation, feedback, and session-history pages
- Server-side Gemini integration through `https://api.openai-proxy.org/google`
- Server-side Suno generation, task persistence, and polling-based status refresh support
- Feedback loop that saves the rating/note and creates a new track version
- Suno audio playback when an audio URL is ready, with synthesized preview audio as a fallback while generation is pending

## Run

```bash
python3 server.py
```

Then open [http://localhost:5173](http://localhost:5173).

The SQLite database is created at `data/neuorise.sqlite3`.

## API Integration Notes

Create a `.env` file with:

```bash
GEMINI_APIKEY=your_gemini_key
SUNO_APIKEY=your_suno_key
```

Optional production settings:

```bash
APP_BASE_URL=https://your-domain.example
SUNO_BASE_URL=https://api.sunoapi.org/api/v1
GEMINI_MODEL=gemini-2.5-flash
SUNO_MODEL=V4_5ALL
```

The app uses polling to check Suno task status via the `/api/v1/generate/record-info` endpoint. The frontend periodically calls `/api/tracks/{id}/refresh` to update track status and retrieve generated audio URLs.

Keep provider credentials in server environment variables. Do not put provider credentials in browser JavaScript.
