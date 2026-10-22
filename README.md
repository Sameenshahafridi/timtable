# AI Study Timetable Generator

Enter a **book name** plus your **days** and **hours per day**, and Google
**Gemini Flash** creates a day-by-day study timetable. Built with **Streamlit**.

## Files (all three go in the ROOT of your GitHub repo)
- `app.py`
- `requirements.txt`
- `README.md`

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
4. **Advanced settings**:
   - Python version: **3.12**
   - Secrets: `GEMINI_API_KEY = "your_key"`
5. Click **Deploy**.

> The Python version can only be chosen when creating the app. If you already
> deployed on a newer Python and packages are missing, delete the app and
> create it again with Python 3.12.

## Troubleshooting
- **`cannot import name 'genai'`** -> `requirements.txt` was not installed.
  Check it is in the repo root, named exactly `requirements.txt`, saved as
  UTF-8 plain text (not UTF-16), and that the repo has no `Pipfile`,
  `pyproject.toml` or `environment.yml`.
- **Model error** -> set `GEMINI_MODEL = "gemini-2.5-flash"` in Secrets.

## Configuration
| Setting | Where | Default |
|---|---|---|
| `GEMINI_API_KEY` | secrets / env / sidebar | - |
| `GEMINI_MODEL` | secrets / env | `gemini-flash-latest` |
