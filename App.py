"""AI Study Timetable Generator
Enter a book name + how much time you have -> get a day-by-day study plan.
UI: Streamlit  |  AI: Google Gemini Flash (google-genai SDK)
"""
import json
import os
import re
from datetime import date, timedelta

import importlib
import subprocess
import sys

import pandas as pd
import streamlit as st


def _ensure_packages():
    """Safety net: if Streamlit Cloud did not install requirements.txt,
    try to install the needed packages at start-up."""
    try:
        importlib.import_module("google.genai")
        importlib.import_module("pydantic")
        return
    except ImportError:
        pass
    pkgs = ["google-genai", "pydantic"]
    commands = [
        [sys.executable, "-m", "pip", "install", "--quiet", *pkgs],
        ["uv", "pip", "install", "--quiet", "--python", sys.executable, *pkgs],
    ]
    for cmd in commands:
        try:
            subprocess.check_call(cmd, timeout=300)
            importlib.invalidate_caches()
            importlib.import_module("google.genai")
            return
        except Exception:
            continue


_ensure_packages()

try:
    from google import genai
    from google.genai import types
    from pydantic import BaseModel
except ImportError as _import_err:  # show the REAL cause (Streamlit Cloud redacts it)
    import os
    from importlib import metadata

    st.error(f"Import failed: {_import_err!r}")
    st.write("Python version:", sys.version)
    here = os.path.dirname(os.path.abspath(__file__))
    st.write("Files next to app.py:", sorted(os.listdir(here)))
    st.write("Repo root files:", sorted(os.listdir(os.path.dirname(here)))[:30]
             if os.path.basename(here) not in ("timtable",) else "(app.py is in repo root)")
    st.write(
        "Installed packages related to google/genai/pydantic:",
        sorted(
            f"{d.metadata['Name']}=={d.version}"
            for d in metadata.distributions()
            if d.metadata["Name"]
            and any(k in d.metadata["Name"].lower() for k in ("google", "genai", "pydantic"))
        ),
    )
    st.stop()

# "gemini-flash-latest" always points to the newest Flash model.
# Override with the GEMINI_MODEL secret/env var (e.g. "gemini-2.5-flash").
DEFAULT_MODEL = "gemini-flash-latest"


# --------------------------------------------------------------------------
# Data models (also used as the JSON schema Gemini must follow)
# --------------------------------------------------------------------------
class StudySession(BaseModel):
    day: int            # 1-based day number
    topic: str          # chapters / topics covered
    activities: str     # what to do (read, notes, practice, revise...)
    duration_minutes: int


class StudyPlan(BaseModel):
    overview: str
    sessions: list[StudySession]
    tips: list[str]


# --------------------------------------------------------------------------
# Core logic (no Streamlit calls here, so it is easy to test)
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


def _parse_plan(response) -> StudyPlan:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, StudyPlan):
        return parsed
    text = (getattr(response, "text", "") or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)  # strip code fences
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return StudyPlan.model_validate(json.loads(text))


def generate_plan(api_key, model, prompt, retries=2) -> StudyPlan:
    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=StudyPlan,
        temperature=0.4,
    )
    last_err = None
    for _ in range(retries):
        try:
            response = client.models.generate_content(
                model=model, contents=prompt, config=config
            )
            return _parse_plan(response)
        except Exception as e:  # network, quota, bad JSON ...
            last_err = e
    raise RuntimeError(f"Could not generate a plan: {last_err}")


def plan_to_dataframe(plan: StudyPlan, start: date, days: int, hours_per_day: float):
    """Clean the AI output and return (DataFrame, warnings)."""
    budget = int(round(hours_per_day * 60))
    rows = []
    for s in plan.sessions:
        day = min(max(int(s.day), 1), days)           # clamp to valid range
        mins = max(int(s.duration_minutes), 0)
        rows.append(
            {
                "Day": day,
                "Date": (start + timedelta(days=day - 1)).strftime("%a, %d %b %Y"),
                "Topic": s.topic.strip(),
                "Activities": s.activities.strip(),
                "Minutes": mins,
            }
        )
    df = pd.DataFrame(rows, columns=["Day", "Date", "Topic", "Activities", "Minutes"])
    if df.empty:
        return df, ["The AI returned no sessions. Try again."]
    df = df.sort_values(["Day"], kind="stable").reset_index(drop=True)

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


def plan_to_markdown(book, plan: StudyPlan, df: pd.DataFrame) -> str:
    lines = [f"# Study Timetable: {book}", "", plan.overview, ""]
    for day, g in df.groupby("Day"):
        lines.append(f"## Day {day} - {g['Date'].iloc[0]}")
        for _, r in g.iterrows():
            lines.append(f"- **{r['Topic']}** ({r['Minutes']} min): {r['Activities']}")
        lines.append("")
    if plan.tips:
        lines.append("## Tips")
        lines += [f"- {t}" for t in plan.tips]
    return "\n".join(lines)


def get_api_key(sidebar_value: str):
    """Priority: Streamlit secrets -> environment variable -> sidebar input."""
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass  # no secrets.toml locally - that's fine
    return os.getenv("GEMINI_API_KEY") or sidebar_value or None


def get_model():
    try:
        if "GEMINI_MODEL" in st.secrets:
            return st.secrets["GEMINI_MODEL"]
    except Exception:
        pass
    return os.getenv("GEMINI_MODEL", DEFAULT_MODEL)


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
            "Gemini API key", type="password",
            help="Get a free key at https://aistudio.google.com/apikey. "
                 "Not needed if it is set in Streamlit secrets.",
        )
        st.caption(f"Model: `{get_model()}`")

    with st.form("inputs"):
        book = st.text_input("Book name", placeholder="e.g. Atomic Habits, Clean Code, Campbell Biology")
        c1, c2, c3 = st.columns(3)
        days = c1.number_input("Number of days", min_value=1, max_value=90, value=7, step=1)
        hours = c2.number_input("Hours per day", min_value=0.5, max_value=12.0, value=2.0, step=0.5)
        start = c3.date_input("Start date", value=date.today())
        c4, c5 = st.columns(2)
        goal = c4.selectbox("Goal", ["Read and understand the whole book", "Exam preparation",
                                     "Quick revision", "Deep study with practice"])
        level = c5.selectbox("Your level", ["Beginner", "Intermediate", "Advanced"])
        notes = st.text_area("Extra notes (optional)",
                             placeholder="e.g. focus on chapters 1-5, I study best in the morning")
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
                    plan = generate_plan(api_key, get_model(), prompt)
                    df, warnings = plan_to_dataframe(plan, start, int(days), float(hours))
                    st.session_state["result"] = {
                        "book": book.strip(), "plan": plan, "df": df, "warnings": warnings,
                    }
                except Exception as e:
                    st.session_state.pop("result", None)
                    st.error(str(e))

    # Result lives in session_state so it survives the rerun caused by download buttons.
    result = st.session_state.get("result")
    if result:
        plan, df = result["plan"], result["df"]
        st.subheader(f"Plan for: {result['book']}")
        st.info(plan.overview)
        for w in result["warnings"]:
            st.warning(w)

        if not df.empty:
            total_h = df["Minutes"].sum() / 60
            m1, m2 = st.columns(2)
            m1.metric("Total study time", f"{total_h:.1f} h")
            m2.metric("Sessions", len(df))

            tab_days, tab_table = st.tabs(["Day by day", "Table"])
            with tab_days:
                for day, g in df.groupby("Day"):
                    with st.expander(f"Day {day} - {g['Date'].iloc[0]}  ({g['Minutes'].sum()} min)",
                                     expanded=(day == 1)):
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

        if plan.tips:
            st.subheader("Study tips")
            for t in plan.tips:
                st.markdown(f"- {t}")


if __name__ == "__main__":
    main()
