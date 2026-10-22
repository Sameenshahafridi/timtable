"""AI Study Timetable Generator
Enter a book name + how much time you have -> get a day-by-day study plan.

UI : Streamlit
AI : Google Gemini Flash, called through the official REST API.
     No google-genai / pydantic needed - only Python's standard library,
     Streamlit and pandas, so there is nothing extra to install.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

import pandas as pd
import streamlit as st

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Tried in order. Override with the GEMINI_MODEL secret / environment variable.
DEFAULT_MODELS = ["gemini-flash-latest", "gemini-2.5-flash"]

# JSON structure we ask Gemini to return
PLAN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "overview": {"type": "STRING"},
        "sessions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "day": {"type": "INTEGER"},
                    "topic": {"type": "STRING"},
                    "activities": {"type": "STRING"},
                    "duration_minutes": {"type": "INTEGER"},
                },
                "required": ["day", "topic", "activities", "duration_minutes"],
            },
        },
        "tips": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["overview", "sessions", "tips"],
}


class GeminiError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Core logic (no Streamlit calls, easy to test)
# --------------------------------------------------------------------------
def build_prompt(book, goal, level, days, hours_per_day, notes):
    minutes = int(round(hours_per_day * 60))
    return f"""You are an expert study coach. Create a study timetable for the book below.

Book: {book}
Goal: {goal}
Student level: {level}
Number of days: {days}
Study time available per day: {hours_per_day} hours ({minutes} minutes)
Extra notes from student: {notes or "None"}

Rules:
- Use your knowledge of the book's real chapters/structure. If you are not sure
  the book exists or do not know its contents, build a sensible plan from the
  title and say so in the overview.
- Return sessions for day 1 to day {days}. Each day may have 1-4 sessions.
- The sum of duration_minutes for any single day must NOT exceed {minutes}.
- Spread the content evenly; include short revision sessions and a final
  review/self-test near the end.
- 'topic' = chapters or topics for that session. 'activities' = concrete
  actions (read, summarise, practice questions, flashcards, etc.).
- Provide 3-5 short, practical study tips.
"""


def call_gemini(api_key, model, prompt, timeout=90):
    """One call to the Gemini REST API. Returns the response text."""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "responseMimeType": "application/json",
            "responseSchema": PLAN_SCHEMA,
        },
    }
    req = urllib.request.Request(
        API_URL.format(model=model),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8"))["error"]["message"]
        except Exception:
            msg = e.reason if hasattr(e, "reason") else "request failed"
        raise GeminiError(f"Gemini API error {e.code}: {msg}", e.code)
    except urllib.error.URLError as e:
        raise GeminiError(f"Network error: {e.reason}")
    except TimeoutError:
        raise GeminiError("The request to Gemini timed out.")

    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
        raise GeminiError(f"Gemini returned no answer ({reason}).")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise GeminiError("Gemini returned an empty answer.")
    return text


def parse_plan(text):
    """Turn Gemini's JSON text into a clean dict. Raises ValueError if unusable."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        raise ValueError("Response did not contain a 'sessions' list.")
    sessions = []
    for s in data["sessions"]:
        try:
            sessions.append(
                {
                    "day": int(s["day"]),
                    "topic": str(s["topic"]).strip(),
                    "activities": str(s.get("activities", "")).strip(),
                    "duration_minutes": int(s["duration_minutes"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed sessions
    if not sessions:
        raise ValueError("No valid sessions in the response.")
    tips = [str(t) for t in data.get("tips", []) if t]
    return {"overview": str(data.get("overview", "")), "sessions": sessions, "tips": tips}


def generate_plan(api_key, models, prompt):
    """Try each model; retry once on temporary errors / bad JSON."""
    last_err = None
    for model in models:
        for attempt in range(2):
            try:
                return parse_plan(call_gemini(api_key, model, prompt))
            except GeminiError as e:
                last_err = e
                if e.code == 404:                      # model name not available
                    break                              # -> try next model
                if e.code in (429, 500, 503) and attempt == 0:
                    time.sleep(2)
                    continue
                raise                                  # bad key, blocked, network...
            except (ValueError, json.JSONDecodeError) as e:
                last_err = e                           # bad JSON -> retry once
    raise RuntimeError(f"Could not generate a plan: {last_err}")


def plan_to_dataframe(plan, start, days, hours_per_day):
    """Clean the AI output and return (DataFrame, warnings)."""
    budget = int(round(hours_per_day * 60))
    rows = []
    for s in plan["sessions"]:
        day = min(max(s["day"], 1), days)                  # clamp to valid range
        rows.append(
            {
                "Day": day,
                "Date": (start + timedelta(days=day - 1)).strftime("%a, %d %b %Y"),
                "Topic": s["topic"],
                "Activities": s["activities"],
                "Minutes": max(s["duration_minutes"], 0),
            }
        )
    df = pd.DataFrame(rows, columns=["Day", "Date", "Topic", "Activities", "Minutes"])
    if df.empty:
        return df, ["The AI returned no sessions. Try again."]
    df = df.sort_values("Day", kind="stable").reset_index(drop=True)

    warnings = []
    per_day = df.groupby("Day")["Minutes"].sum()
    for d, total in per_day.items():
        if total > budget * 1.1:
            warnings.append(f"Day {d} is planned for {total} min (your limit is {budget} min).")
    missing = [d for d in range(1, days + 1) if d not in per_day.index]
    if missing:
        shown = ", ".join(map(str, missing[:10])) + ("..." if len(missing) > 10 else "")
        warnings.append(f"No sessions were planned for day(s): {shown}.")
    return df, warnings


def plan_to_markdown(book, plan, df):
    lines = [f"# Study Timetable: {book}", "", plan["overview"], ""]
    for day, g in df.groupby("Day"):
        lines.append(f"## Day {day} - {g['Date'].iloc[0]}")
        for _, r in g.iterrows():
            lines.append(f"- **{r['Topic']}** ({r['Minutes']} min): {r['Activities']}")
        lines.append("")
    if plan["tips"]:
        lines.append("## Tips")
        lines += [f"- {t}" for t in plan["tips"]]
    return "\n".join(lines)


def _secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass  # no secrets file locally - fine
    return None


def get_api_key(sidebar_value):
    """Priority: Streamlit secrets -> environment variable -> sidebar input."""
    return _secret("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") or sidebar_value or None


def get_models():
    custom = _secret("GEMINI_MODEL") or os.getenv("GEMINI_MODEL")
    return [custom] if custom else list(DEFAULT_MODELS)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="AI Study Timetable", page_icon="📚", layout="wide")
    st.title("📚 AI Study Timetable Generator")
    st.caption("Enter a book and your available time - Gemini builds a day-by-day plan.")

    with st.sidebar:
        st.header("Settings")
        sidebar_key = st.text_input(
            "Gemini API key",
            type="password",
            help="Get a free key at https://aistudio.google.com/apikey. "
                 "Not needed if it is set in Streamlit secrets.",
        )
        st.caption("Model: " + " -> ".join(get_models()))

    with st.form("inputs"):
        book = st.text_input("Book name", placeholder="e.g. Atomic Habits, Clean Code, Campbell Biology")
        c1, c2, c3 = st.columns(3)
        days = c1.number_input("Number of days", min_value=1, max_value=90, value=7, step=1)
        hours = c2.number_input("Hours per day", min_value=0.5, max_value=12.0, value=2.0, step=0.5)
        start = c3.date_input("Start date", value=date.today())
        c4, c5 = st.columns(2)
        goal = c4.selectbox(
            "Goal",
            ["Read and understand the whole book", "Exam preparation",
             "Quick revision", "Deep study with practice"],
        )
        level = c5.selectbox("Your level", ["Beginner", "Intermediate", "Advanced"])
        notes = st.text_area(
            "Extra notes (optional)",
            placeholder="e.g. focus on chapters 1-5, I study best in the morning",
        )
        submitted = st.form_submit_button("Generate timetable", type="primary")

    if submitted:
        api_key = get_api_key(sidebar_key)
        if not book.strip():
            st.error("Please enter a book name.")
        elif not api_key:
            st.error("Please provide a Gemini API key (sidebar or Streamlit secrets).")
        else:
            prompt = build_prompt(book.strip(), goal, level, int(days), float(hours), notes.strip())
            with st.spinner("Building your timetable..."):
                try:
                    plan = generate_plan(api_key, get_models(), prompt)
                    df, warnings = plan_to_dataframe(plan, start, int(days), float(hours))
                    st.session_state["result"] = {
                        "book": book.strip(), "plan": plan, "df": df, "warnings": warnings,
                    }
                except Exception as e:
                    st.session_state.pop("result", None)
                    st.error(str(e))

    # Result lives in session_state so it survives reruns caused by download buttons.
    result = st.session_state.get("result")
    if result:
        plan, df = result["plan"], result["df"]
        st.subheader(f"Plan for: {result['book']}")
        if plan["overview"]:
            st.info(plan["overview"])
        for w in result["warnings"]:
            st.warning(w)

        if not df.empty:
            m1, m2 = st.columns(2)
            m1.metric("Total study time", f"{df['Minutes'].sum() / 60:.1f} h")
            m2.metric("Sessions", len(df))

            tab_days, tab_table = st.tabs(["Day by day", "Table"])
            with tab_days:
                for day, g in df.groupby("Day"):
                    with st.expander(
                        f"Day {day} - {g['Date'].iloc[0]}  ({g['Minutes'].sum()} min)",
                        expanded=(day == 1),
                    ):
                        for _, r in g.iterrows():
                            st.markdown(f"**{r['Topic']}** - {r['Minutes']} min  \n{r['Activities']}")
            with tab_table:
                st.dataframe(df, hide_index=True)

            d1, d2 = st.columns(2)
            d1.download_button("⬇️ Download CSV", df.to_csv(index=False).encode("utf-8"),
                               file_name="study_timetable.csv", mime="text/csv")
            d2.download_button("⬇️ Download Markdown",
                               plan_to_markdown(result["book"], plan, df).encode("utf-8"),
                               file_name="study_timetable.md", mime="text/markdown")

        if plan["tips"]:
            st.subheader("Study tips")
            for t in plan["tips"]:
                st.markdown(f"- {t}")


if __name__ == "__main__":
    main()
