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

## Product Workflow

The core workflow of this product is as follows:

1. **User State Collection:** Gauge the user's mood and state through a short questionnaire, with the number of questions controlled between 5–7. Concurrently detect basic physiological data such as heart rate and respiration rate.
2. **Prompt Generation:** Feed the questionnaire answers and basic physiological data back to **Gemini**. The large language model (LLM) will then assist in generating a music generation prompt.
3. **Music Generation:** Copy the generated prompt into **Suno** to generate healing music that matches the user's current state.
4. **User Feedback and Re-generation:** Users provide feedback on whether the music meets their needs and whether it has a healing effect through actions like skipping songs or rating (1–5 points). The system will continue to generate or adjust the music based on this feedback.

This generation method first collects the user's demand for music, then uses an LLM to guide the music generation, and finally collects user feedback to better satisfy user needs. This process adopts MindMelody's approach of using an LLM as music guidance and forms a closed loop for the entire generation process. When users provide feedback on music alignment, it also deepens the generation engine's understanding of user needs.

## Product Positioning and Application Goals

- **Product Positioning:** The target audience consists of patients with mild-to-moderate depression. The product aims to enhance the personalized capability of music therapy to assist in clinical treatment.
- **Ideal Application Scenario:** To play an auxiliary/supportive role during psychological counseling or clinical medical processes.
- **Ideal Output/Effect:** Output continuous music; after new user feedback is given, the system can gradually transition to new, compliant music while reasonably regulating decibel levels. The goal is to assist users in alleviating anxiety or depression without noticeable shifts.

## Input and Output Parameter Settings

### Input Parameter Settings

- User's answers to the short questionnaire
- User's basic physiological data
- Current decibel value

### Output (LLM) Parameter Settings

- BPM
- Music style
- Musical instruments
- Melody consistency prompts

### Comprehensive Research

1. Research on the mapping between the emotional circumplex model and musical parameters
2. Research on the auditory entrainment effect and seamless audio crossfading

---

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
