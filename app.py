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
DEFAULT_MODELS = [
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
]
RETRYABLE = (500, 502, 503, 504)  # temporary Google-side problems (429 handled separately)

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


class ServiceBusy(RuntimeError):
    """Every model was overloaded, out of quota or unavailable (temporary problem).
    quota=True means the failures were all free-tier quota (429) errors."""

    def __init__(self, message, quota=False):
        super().__init__(message)
        self.quota = quota


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


def _is_daily_quota(msg):
    """True when a 429 says the DAILY free quota is gone (waiting a few seconds won't help)."""
    m = msg.lower()
    return "per day" in m or "perday" in m.replace(" ", "") or bool(re.search(r"retry in \d+h", m))


def generate_plan(api_key, models, prompt):
    """Try each model in turn.
    - 429 daily quota  -> skip straight to the next model (each model has its own quota)
    - 429 per-minute   -> wait briefly, retry once, then next model
    - 503/500/...      -> wait, retry once, then next model
    - 404 / bad JSON / network blip -> retry once, then next model
    Raises ServiceBusy if every model failed temporarily, or GeminiError for
    permanent problems such as an invalid API key."""
    last_err = None
    saw_quota = False
    saw_other = False
    for i, model in enumerate(models):
        for attempt in range(2):
            try:
                return parse_plan(call_gemini(api_key, model, prompt))
            except GeminiError as e:
                last_err = e
                if e.code == 429:
                    saw_quota = True
                    if _is_daily_quota(str(e)) or attempt == 1:
                        break                          # -> next model
                    time.sleep(3)
                    continue
                if e.code == 404:
                    break                              # model not available -> next
                saw_other = True
                if e.code is None or e.code in RETRYABLE:
                    if attempt == 0:
                        time.sleep(2 + 2 * i)          # simple back-off
                        continue
                    break                              # -> next model
                raise                                  # invalid key, bad request...
            except (ValueError, json.JSONDecodeError) as e:
                last_err = e                           # bad JSON -> retry / next model
                saw_other = True
    raise ServiceBusy(f"Gemini is unavailable right now ({last_err}).",
                      quota=saw_quota and not saw_other)


@st.cache_data(ttl=24 * 3600, show_spinner=False, max_entries=200)
def cached_generate_plan(api_key, models, prompt):
    """Same request within 24h = no new Gemini call (saves your free quota).
    Errors are never cached."""
    return generate_plan(api_key, list(models), prompt)


def build_fallback_plan(book, days, hours_per_day):
    """Simple built-in timetable used only when Gemini is unavailable."""
    budget = int(round(hours_per_day * 60))
    read_m, note_m = int(budget * 0.6), int(budget * 0.25)
    recap_m = budget - read_m - note_m
    sessions = []
    if days == 1:
        sessions = [
            {"day": 1, "topic": "Survey the book", "activities": "Read the contents page, introduction and chapter headings.", "duration_minutes": recap_m},
            {"day": 1, "topic": "Core reading", "activities": "Read the main chapters actively and mark key ideas.", "duration_minutes": read_m},
            {"day": 1, "topic": "Summary and self-test", "activities": "Write a one-page summary and quiz yourself.", "duration_minutes": note_m},
        ]
    else:
        n = days - 1                                    # reading days; last day = revision
        for i in range(1, n + 1):
            lo, hi = round((i - 1) * 100 / n), round(i * 100 / n)
            part = f"Part {i} of {n} (about {lo}%-{hi}% of the book)"
            sessions += [
                {"day": i, "topic": part, "activities": "Read actively; mark key ideas and questions." + (" Start by skimming the contents page." if i == 1 else ""), "duration_minutes": read_m},
                {"day": i, "topic": part + " - notes", "activities": "Write a short summary in your own words and list key terms.", "duration_minutes": note_m},
                {"day": i, "topic": "Quick recap", "activities": "Quiz yourself on today's part without looking at your notes.", "duration_minutes": recap_m},
            ]
        sessions += [
            {"day": days, "topic": "Revision of all parts", "activities": "Review your notes and re-read the most important sections.", "duration_minutes": int(budget * 0.5)},
            {"day": days, "topic": "Self-test", "activities": "Answer questions on the key ideas from memory.", "duration_minutes": int(budget * 0.35)},
            {"day": days, "topic": "Next steps", "activities": "List what to review again and how to apply what you learned.", "duration_minutes": budget - int(budget * 0.5) - int(budget * 0.35)},
        ]
    return {
        "overview": f"Basic template plan for \"{book}\" (Gemini was busy, so this plan is generic and not based on the book's real chapters).",
        "sessions": sessions,
        "tips": ["Study in focused blocks and take short breaks.",
                 "Test yourself instead of only re-reading.",
                 "Click Generate again later to get a Gemini-made plan."],
    }


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
    """Priority: key typed in the sidebar -> Streamlit secrets -> environment variable."""
    return (sidebar_value or "").strip() or _secret("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") or None


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
                 "Optional if a key is set in Streamlit secrets; a key typed here takes priority. "
                 "Free keys have a small daily limit - a new key from a new Google project resets it.",
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
            with st.spinner("Building your timetable (this can take up to a minute if Gemini is busy)..."):
                try:
                    notice = None
                    try:
                        plan = cached_generate_plan(api_key, tuple(get_models()), prompt)
                    except ServiceBusy as busy:
                        plan = build_fallback_plan(book.strip(), int(days), float(hours))
                        if busy.quota:
                            notice = ("Your free Gemini quota is used up for now (the free tier allows "
                                      "only a few requests per day per model), so a basic template "
                                      "timetable is shown. Try again later, or paste a different API "
                                      "key in the sidebar.")
                        else:
                            notice = ("Google's Gemini servers are overloaded right now, so a basic "
                                      "template timetable is shown. Click Generate again in a minute "
                                      "for a personalised AI plan.")
                    df, warnings = plan_to_dataframe(plan, start, int(days), float(hours))
                    if notice:
                        warnings.insert(0, notice)
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
