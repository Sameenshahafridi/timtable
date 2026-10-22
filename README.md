# 📚 AI Study Timetable Generator

Enter a **book name** and how much **time** you have (days + hours per day), and
Google **Gemini Flash** builds a day-by-day study timetable. Built with **Streamlit**.

## Features
- Book name, number of days, hours/day, start date, goal and level
- Structured AI output (validated JSON) -> clean timetable
- Warnings if a day exceeds your time limit or a day is empty
- Day-by-day view + table view
- Download as CSV or Markdown

## Run locally
```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Get a free API key: https://aistudio.google.com/apikey

Provide the key in **one** of these ways:
1. Paste it in the app sidebar, or
2. Environment variable: `export GEMINI_API_KEY="your_key"` (Windows: `set GEMINI_API_KEY=your_key`), or
3. Create `.streamlit/secrets.toml`:
   ```toml
   GEMINI_API_KEY = "your_key"
   # GEMINI_MODEL = "gemini-2.5-flash"   # optional override
   ```

Run:
```bash
streamlit run app.py
```

## Deploy on Streamlit Community Cloud
1. Push this repo to GitHub (never commit your API key).
2. Go to https://share.streamlit.io and sign in with GitHub.
3. Click **Create app**, choose your repo, branch `main`, main file `app.py`.
4. Open **Advanced settings -> Secrets** and paste: `GEMINI_API_KEY = "your_key"`
5. Click **Deploy**.

## Configuration
| Setting | Where | Default |
|---|---|---|
| `GEMINI_API_KEY` | secrets / env / sidebar | - |
| `GEMINI_MODEL` | secrets / env | `gemini-flash-latest` |

## Notes
- The AI may not know obscure books; add chapter names in "Extra notes" for better plans.
- Always review the plan - AI can make mistakes.
