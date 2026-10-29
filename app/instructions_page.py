"""Retrieval instructions: the same agentic search, with and without a reviewed instruction (persona,
retrieval guidance, answer format, answer policy).

The instructions are deployed by the stack (lib/retrieval-instructions.ts). This page only reads them (GetRetrievalInstruction) and applies them.
"""
import streamlit as st
from botocore.exceptions import ClientError

from common import (RETRIEVAL_INSTRUCTIONS, agentic_result_rows, agentic_retrieve, bedrock_agent, current_user,
                    with_citation_marks)

FIELDS = ["retrievalGuidance", "persona", "answerFormat", "answerPolicy"]


@st.cache_data(ttl=60)
def describe(instruction_id: str) -> dict:
    r = bedrock_agent.get_retrieval_instruction(retrievalInstructionId=instruction_id)
    r.pop("ResponseMetadata", None)
    return r


def column(title: str, run: dict, instruction_id: str | None) -> None:
    st.subheader(title)
    applied = run["applied"]
    if instruction_id:
        if applied and applied.get("id") == instruction_id:
            st.success(f"applied instruction `{applied['id']}` version `{applied.get('version')}`",
                       icon=":material/verified:")
        else:
            st.error(f"instruction not reported as applied (got {applied})")
    planned = list(dict.fromkeys(q for s in run["steps"] for q in s["queries"]))
    st.caption(f"{len(run['results'])} results · {len(planned)} sub-queries · {run['seconds']}s")
    st.markdown(with_citation_marks(run["answer"], run["citations"]) or "_no answer_")
    with st.expander(f"Sub-queries ({len(planned)})"):
        st.markdown("\n".join(f"- {q}" for q in planned) or "none")
    with st.expander(f"Results ({len(run['results'])})"):
        st.dataframe(agentic_result_rows(run["results"]), hide_index=True, width="stretch",
                     column_config={"text": st.column_config.TextColumn("chunk", width="large")})


user = current_user()
st.title("Retrieval instructions")
st.caption("A retrieval instruction is a reviewed, versioned resource that steers agentic retrieval: "
           "**retrievalGuidance** (how to search), **persona** (who answers), **answerFormat** (layout) and "
           "**answerPolicy** (what to decline). Each question runs twice: baseline, then with the instruction.")

if not RETRIEVAL_INSTRUCTIONS:
    st.warning("The stack deployed no retrieval instructions (RETRIEVAL_INSTRUCTIONS is empty).")
    st.stop()

by_label = {ri["label"]: ri for ri in RETRIEVAL_INSTRUCTIONS}
ri = by_label[st.segmented_control("Instruction", list(by_label), default=next(iter(by_label)))
              or next(iter(by_label))]

with st.sidebar:
    st.subheader("Instruction settings")
    version = st.text_input("Version", "", placeholder="DRAFT",
                            help="Empty = DRAFT (the current reviewed content). A number pins a published version.")
    baseline = st.toggle("Run the baseline (no instruction)", True)
    iterations = st.slider("Max agent iterations", 1, 10, 3)

try:
    info = describe(ri["id"])
except ClientError as e:
    st.error(f"{e.response['Error']['Code']}: {e.response['Error']['Message']}")
    st.stop()
with st.expander(f"`{info['name']}` · {info['status']} · {info.get('lastTransitionStatus')} · "
                 f"latest version {info.get('latestVersion') or 'none'}"):
    for f in FIELDS:
        if info.get("instruction", {}).get(f):
            st.markdown(f"**{f}**")
            st.write(info["instruction"][f])
if info["status"] != "ACTIVE":
    st.warning("The instruction isn't ACTIVE yet (review in progress or rejected); requests that apply it fail.")

st.caption("Demo questions: the first is in scope; the second should be redirected or declined by the answerPolicy.")
ex = st.pills("Demo questions", ri["queries"], label_visibility="collapsed", key=f"pills-{ri['key']}")
with st.form("instruction"):
    query = st.text_input("Question", ex or "")
    go = st.form_submit_button("Compare", type="primary")

if go and query:
    runs = ([("Baseline (no instruction)", None)] if baseline else []) + [(ri["label"], ri["id"])]
    results = []
    try:
        for title, rid in runs:
            with st.spinner(f"{title}..."):
                results.append((title, rid, agentic_retrieve(query, user or None, iterations=iterations,
                                                             instruction_id=rid, version=version.strip() or None)))
    except ClientError as e:
        st.error(f"{e.response['Error']['Code']}: {e.response['Error']['Message']}")
    st.session_state.instruction_last = {"query": query, "key": ri["key"], "results": results}

last = st.session_state.get("instruction_last")
if last and last["key"] == ri["key"] and last["results"]:
    st.caption(f"“{last['query']}”")
    for (title, rid, run), col in zip(last["results"], st.columns(len(last["results"]))):
        with col:
            column(title, run, rid)
