# Streamlit Community Deployment

Use this checklist to deploy the app from your fork.

## Repository Settings

- Repository: `tuannm3812/aipa-text-to-sql-agent`
- Branch: `tuannm3812/main-refinement`
- Main file path: `app.py`
- Python version: choose Python 3.12 in Streamlit Community Cloud advanced settings

## Secrets

For Gemini deployment, add this in Streamlit Community Cloud secrets:

```toml
GEMINI_API_KEY = "your_key_here"
TEXT_TO_SQL_PROVIDER = "gemini"
```

Do not commit `.env` or `.streamlit/secrets.toml`.

## Notes

- Gemini is suitable for Streamlit Community Cloud because it calls a hosted API.
- Ollama is suitable for local demos. Streamlit Community Cloud does not provide your local Ollama server, so local models should be presented as a local/offline option rather than the hosted default.
- The app has `requirements.txt` at the repository root and `app.py` at the repository root, which matches Streamlit Community Cloud's expected project layout.

## Deploy Steps

1. Go to `https://share.streamlit.io`.
2. Create a new app from an existing GitHub repository.
3. Select repository, branch, and `app.py`.
4. Add secrets in advanced settings.
5. Deploy and check the app logs.
