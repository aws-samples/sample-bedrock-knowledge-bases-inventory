"""Agentic search: AgenticRetrieveStream plans sub-queries, retrieves in several rounds, can expand whole
documents, and can write a cited answer.

Shows the live trace, the answer with citation marks that point into the results table, and optionally
the no-user ACL control.
"""
import pandas as pd
import streamlit as st
from botocore.exceptions import ClientError

from common import agentic_result_rows, agentic_retrieve, current_user, label, with_citation_marks

EXAMPLES = [
    "Which was hotter in July 2024, Phoenix or Chicago, and which got more rain?",
    "Compare the coldest January 2024 day in Minneapolis, Denver and Beijing.",
    "Which of Heathrow, Schiphol and Frankfurt had the most foggy days in November 2024?",
    "Was Sydney or Johannesburg windier in August 2024?",
]


def render_trace(steps: list[dict], box) -> None:
    lines = []
    for s in steps:
        line = f"**{s['step']}** · {s['status']}"
        if s["queries"]:
            line += " · sub-queries: " + "; ".join(f"“{q}”" for q in s["queries"])
        if s["expanded"]:
            line += " · expand: " + ", ".join(label(d) for d in s["expanded"])
        if s["failures"]:
            line += " · :red[" + "; ".join(s["failures"]) + "]"
        lines.append("- " + line)
    box.markdown("\n".join(lines[-40:]))


def show(run: dict) -> None:
    planned = list(dict.fromkeys(q for s in run["steps"] for q in s["queries"]))
    expanded = {d for s in run["steps"] for d in s["expanded"]}
    c = st.columns(4)
    c[0].metric("Results", len(run["results"]))
    c[1].metric("Sub-queries", len(planned))
    c[2].metric("Full-document expansions", len(expanded))
    c[3].metric("Seconds", run["seconds"])
    if run["answer"]:
        st.markdown(with_citation_marks(run["answer"], run["citations"]))
    if planned:
        with st.expander(f"Planned sub-queries ({len(planned)})"):
            st.markdown("\n".join(f"- {q}" for q in planned))
    df = agentic_result_rows(run["results"])
    cited = {ref["resultIndex"] + 1 for c in run["citations"] for ref in c.get("references", [])}
    df.insert(1, "cited", df["#"].isin(cited))
    with st.expander(f"Results ({len(df)}, {len(cited)} cited)"):
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"text": st.column_config.TextColumn("chunk", width="large")})


user = current_user()
st.title("Agentic search")
st.caption("`AgenticRetrieveStream`: a service-managed model plans sub-queries (e.g. one per city), retrieves "
           f"in rounds, expands whole station files when it needs totals, and writes a cited answer. As `{user or 'nobody'}`.")

with st.sidebar:
    st.subheader("Agentic settings")
    max_results = st.slider("Max results per retrieval", 1, 25, 10)
    iterations = st.slider("Max agent iterations", 1, 10, 3)
    answer = st.toggle("Generate an answer", True, help="Off: results only (faster, no answer model).")
    control = st.toggle("Also run the no-user control", False,
                        help="Same request without userContext; must return 0 results.")

ex = st.pills("Examples", EXAMPLES, label_visibility="collapsed")
with st.form("agentic"):
    query = st.text_input("Question", ex or "", placeholder="e.g. which was wetter in 2024, Seattle or Miami?")
    go = st.form_submit_button("Run agentic search", type="primary")

if go and query:
    if not user:
        st.warning("Set **Ask as** in the sidebar; without a user the KB returns nothing.")
    try:
        with st.status("Planning and retrieving...", expanded=True) as status:
            trace_box, text_box, steps = st.empty(), st.empty(), []

            def on_trace(step):
                steps.append(step)
                render_trace(steps, trace_box)

            run = agentic_retrieve(query, user or None, max_results=max_results, iterations=iterations,
                                   answer=answer, on_trace=on_trace, on_text=lambda t: text_box.markdown(t + "▌"))
            text_box.empty()
            status.update(label=f"Trace: {len(run['steps'])} events, {run['seconds']}s", state="complete",
                          expanded=False)
        run["control"] = (len(agentic_retrieve(query, None, max_results=max_results, iterations=1,
                                               answer=False)["results"]) if control else None)
        st.session_state.agentic_last = {"query": query, "user": user, **run}
    except ClientError as e:
        st.error(f"{e.response['Error']['Code']}: {e.response['Error']['Message']}")

# Kept across reruns (e.g. moving a slider), so a result isn't lost or silently re-billed.
last = st.session_state.get("agentic_last")
if last:
    if not go:
        st.caption(f"Last run: “{last['query']}” as `{last['user'] or 'nobody'}`")
        render_trace(last["steps"], st.expander(f"Trace ({len(last['steps'])} events)"))
    show(last)
    if last["control"] is not None:
        msg = f"Control (no userContext): {last['control']} results"
        if last["control"]:
            st.error(msg + ". The data source isn't filtering by ACL.")
        else:
            st.success(msg + ", as expected.")
