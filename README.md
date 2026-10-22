# AI Study Timetable Generator

Enter a **book name** plus your **days** and **hours per day**, and Google
**Gemini Flash** builds a day-by-day study timetable. Built with **Streamlit**.

The app talks to Gemini through its REST API using only Python's standard
library, so the only dependencies are `streamlit` and `pandas`
(no `google-genai` or `pydantic` needed).

## Files (all three go in the ROOT of your GitHub repo)
- `app.py`
- `requirements.txt`
- `README.md`

## Features
- Book name, days, hours/day, start date, goal and level
- Structured JSON output from Gemini, validated and cleaned
- Warnings if a day exceeds your time limit or a day has no sessions
- Day-by-day view and table view
- Download as CSV or Markdown

## Run locally
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
export GEMINI_API_KEY="your_key"   # Windows: set GEMINI_API_KEY=your_key
streamlit run app.py
```
Free API key: https://aistudio.google.com/apikey
(You can also paste the key in the app sidebar.)

## Deploy on Streamlit Community Cloud
1. Push the three files to the root of a GitHub repo.
2. Go to https://share.streamlit.io -> **Create app**.
3. Pick your repo, branch `main`, main file `app.py`.
4. **Advanced settings -> Secrets**: `GEMINI_API_KEY = "your_key"`
5. Click **Deploy**. If you already deployed, just push the new files and
   reboot the app from **Manage app**.

## Configuration
| Setting | Where | Default |
|---|---|---|
| `GEMINI_API_KEY` | secrets / env / sidebar | - |
| `GEMINI_MODEL` | secrets / env | tries `gemini-flash-latest`, then `gemini-2.5-flash` |

## Troubleshooting
- **"API key not valid"** -> create a new key at aistudio.google.com/apikey.
- **429 / quota error** -> wait a minute (free tier limits) and try again.
- **Model not found** -> set `GEMINI_MODEL = "gemini-2.5-flash"` in Secrets.
- Never commit your API key to GitHub.
