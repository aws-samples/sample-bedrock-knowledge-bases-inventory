# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Weather chat: a Claude agent over the Bedrock Knowledge Base, with inventory provenance for every cited document.

Claude (Bedrock Converse, streaming) gets one tool, `search_knowledge_base`, backed by the KB Retrieve API.
Retrieved chunks are numbered [S1], [S2], ... across the conversation; the model cites them inline.
The data source is ACL-enabled, so every Retrieve passes the sidebar "Ask as" email (app.py) as `userContext`; the KB only
returns documents that email is allowed by acl/global-acl.json. The email is not authenticated (demo only).
Each answer's citations are joined to the inventory (KB status, chunks, ingestion time) and written to S3,
so the inventory page can show which documents answers were actually grounded in.
"""
import json
import re
import uuid

import pandas as pd
import streamlit as st

from common import (CHAT_MODELS, DATA_BUCKET, KB_ID, bedrock_agent_runtime, bedrock_runtime, current_user,
                    inventory, label, location_uri, record_citations, to_document_id)

MAX_TOOL_ROUNDS = 5
SYSTEM_PROMPT = """You are a helpful weather assistant answering from an Amazon Bedrock Knowledge Base of NOAA GSOD \
daily weather CSVs from 2020 up to the latest available year (partial), one file per airport station per year. \
Each row is one day: DATE, NAME (station), TEMP/MAX/MIN in Fahrenheit, DEWP, PRCP in inches, WDSP/MXSPD/GUST in \
knots, VISIB in miles, SNDP, and FRSHTT flags (Fog, Rain, Snow, Hail, Thunder, Tornado). 9999.9, 999.9 and 99.99 mean missing. The data is historical: you cannot forecast.

- Always call search_knowledge_base before answering a factual question.
- If the results are partial, truncated or only cover part of the question, search again with narrower queries \
(e.g. one per city or per month) before answering. Prefer a complete answer over a fast one.
- Answer only from the search results. If they don't contain the answer, say so.
- Cite every claim inline with the source ids from the results, e.g. [S3] or [S1][S4]. Never invent ids.
- Be concise; use short paragraphs or bullets."""

TOOLS = {"tools": [{"toolSpec": {
    "name": "search_knowledge_base",
    "description": "Hybrid search over the weather Knowledge Base. Returns numbered source chunks of station CSV rows.",
    "inputSchema": {"json": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "A focused search query."}},
        "required": ["query"],
    }},
}}]}

SUGGESTIONS = [
    "What was the hottest day at Chicago O'Hare in 2024?",
    "Did it snow at Denver International Airport in April 2024?",
    "Compare the rainfall in Seattle and Miami in March 2024.",
    "Which airport had the strongest wind gust in December 2024?",
    "What's the weather in London tomorrow?",
]

ss = st.session_state
ss.setdefault("session_id", uuid.uuid4().hex)
ss.setdefault("messages", [])   # Converse-format history, including toolUse / toolResult blocks
ss.setdefault("display", [])    # [{role, text, sources: [source dicts], searches: [str]}]
ss.setdefault("sources", [])    # every retrieved chunk this conversation; index + 1 = S-number


# ------------------------------------------------------------------ tools
def search_knowledge_base(query: str, k: int, user: str) -> str:
    # Without userContext an ACL-enabled data source returns nothing, so skip the call.
    if not user:
        return "No results: no user identity set, so access-controlled documents are hidden."
    r = bedrock_agent_runtime.retrieve(
        knowledgeBaseId=KB_ID, retrievalQuery={"text": query}, userContext={"userId": user},
        retrievalConfiguration={"managedSearchConfiguration": {"numberOfResults": k}})
    parts = []
    for res in r.get("retrievalResults", []):
        md = res.get("metadata", {})
        # Managed KBs use _-prefixed metadata keys; vector KBs used x-amz-bedrock-kb-*.
        page = md.get("_excerpt_page_number", md.get("x-amz-bedrock-kb-document-page-number"))
        src = {
            "source": f"S{len(ss.sources) + 1}",
            # Any connector: the location is the document's source_uri; map it to the inventory's document_id.
            "uri": to_document_id(location_uri(res.get("location", {}))),
            "page": int(page) if page is not None else None,
            "chunk_id": md.get("_chunk_id", md.get("x-amz-bedrock-kb-chunk-id")),
            "score": round(res.get("score", 0.0), 3),
            "text": res.get("content", {}).get("text", ""),
        }
        ss.sources.append(src)
        parts.append(f"[{src['source']}] ({label(src['uri'])}, score {src['score']})\n{src['text']}")
    return "\n\n".join(parts) or "No results."


# ------------------------------------------------------------- agent loop
def run_agent(model: str, k: int, user: str, out) -> tuple[str, list[str]]:
    """Stream one assistant turn, executing tool calls until the model stops. Returns (text, searches)."""
    text, searches = "", []
    for _ in range(MAX_TOOL_ROUNDS):
        resp = bedrock_runtime.converse_stream(
            modelId=model, messages=ss.messages, system=[{"text": SYSTEM_PROMPT}], toolConfig=TOOLS,
            inferenceConfig={"maxTokens": 2000})
        blocks: dict[int, dict] = {}
        stop = None
        for ev in resp["stream"]:
            if "contentBlockStart" in ev:
                start = ev["contentBlockStart"]
                if "toolUse" in start["start"]:
                    tu = start["start"]["toolUse"]
                    blocks[start["contentBlockIndex"]] = {"toolUse": {**tu, "input": ""}}
            elif "contentBlockDelta" in ev:
                d, i = ev["contentBlockDelta"]["delta"], ev["contentBlockDelta"]["contentBlockIndex"]
                if "text" in d:
                    blocks.setdefault(i, {"text": ""})["text"] += d["text"]
                    text += d["text"]
                    out.markdown(text + "▌")
                elif "toolUse" in d:
                    blocks[i]["toolUse"]["input"] += d["toolUse"].get("input", "")
            elif "messageStop" in ev:
                stop = ev["messageStop"]["stopReason"]

        content = []
        for i in sorted(blocks):
            b = blocks[i]
            if "toolUse" in b:
                b["toolUse"]["input"] = json.loads(b["toolUse"]["input"] or "{}")
            if b.get("text", None) != "":
                content.append(b)
        ss.messages.append({"role": "assistant", "content": content})

        if stop != "tool_use":
            break
        results = []
        for b in content:
            if "toolUse" in b:
                q = b["toolUse"]["input"].get("query", "")
                searches.append(q)
                with st.spinner(f"Searching the knowledge base: {q}"):
                    result = search_knowledge_base(q, k, user)
                results.append({"toolResult": {"toolUseId": b["toolUse"]["toolUseId"], "content": [{"text": result}]}})
        ss.messages.append({"role": "user", "content": results})
        if text:
            text += "\n\n"
    out.markdown(text)
    return text, searches


# ---------------------------------------------------------- provenance UI
def render_sources(text: str, sources: list[dict]) -> None:
    if not sources:
        return
    cited = {f"S{n}" for n in re.findall(r"\[S(\d+)\]", text)}
    shown = [s for s in sources if s["source"] in cited] or sources
    inv = inventory().set_index("uri")
    with st.expander(f"Sources & inventory provenance ({len(shown)} {'cited' if cited else 'retrieved'})",
                     expanded=False):
        for uri, grp in pd.DataFrame(shown).groupby("uri", sort=False):
            row = inv.loc[uri] if uri in inv.index else None
            key = f"{label(uri)} ({uri.replace(f's3://{DATA_BUCKET}/', '')})"
            if row is not None and row.get("kb_status") == "INDEXED":
                chunks = row.get("chunks_created")
                chunks = f"{int(chunks):,} chunks" if pd.notna(chunks) else "chunks n/a"
                when = pd.to_datetime(row.get("kb_updated_at"), utc=True)
                when = f"{when:%Y-%m-%d %H:%M} UTC" if pd.notna(when) else "n/a"
                st.success(f"**{key}** is in the inventory: INDEXED · {chunks} · last ingested {when}"
                           f" · cited {int(row.get('times_cited', 0))}x so far", icon=":material/verified:")
            else:
                st.warning(f"**{key}** is not INDEXED in the current inventory "
                           f"({row.get('kb_status') if row is not None else 'unknown'})", icon=":material/warning:")
            st.caption(f"document_id: `{uri}`")  # the ID the KB ACL APIs expect
            st.dataframe(grp[["source", "page", "score", "text"]], hide_index=True, width="stretch",
                         column_config={"text": st.column_config.TextColumn("chunk", width="large")})


# --------------------------------------------------------------------- UI
with st.sidebar:
    st.subheader("Chat settings")
    model = st.selectbox("Model (Bedrock inference profile)", CHAT_MODELS)
    k = st.slider("Chunks per search", 3, 10, 5)
    if st.button("New conversation"):
        for key in ("messages", "display", "sources"):
            ss[key] = []
        ss.session_id = uuid.uuid4().hex
        st.rerun()

user = current_user()
st.title("Weather chat")
st.caption(f"KB `{KB_ID}` · {model} · as `{user or 'nobody'}` · answers cite retrieved chunks and show each document's inventory status")

for m in ss.display:
    with st.chat_message(m["role"]):
        if m.get("searches"):
            st.caption("Searched: " + " · ".join(f"“{q}”" for q in m["searches"]))
        st.markdown(m["text"])
        render_sources(m["text"], m.get("sources", []))

prompt = st.chat_input("Ask about airport weather (2020 onward)...")
if not ss.display and not prompt:
    st.write("Try one of these:")
    for i, s in enumerate(SUGGESTIONS):
        if st.button(s, key=f"sugg{i}"):
            prompt = s

if prompt:
    ss.display.append({"role": "user", "text": prompt})
    ss.messages.append({"role": "user", "content": [{"text": prompt}]})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        first_source = len(ss.sources)
        answer, searches = run_agent(model, k, user, st.empty())
        new_sources = ss.sources[first_source:]
        cited = {f"S{n}" for n in re.findall(r"\[S(\d+)\]", answer)}
        # Citations can point at chunks retrieved in earlier turns too.
        turn_sources = [s for s in ss.sources if s["source"] in cited] or new_sources
        try:
            record_citations([{"session_id": ss.session_id, "model": model, "user_id": user, "question": prompt,
                               **{f: s[f] for f in ("source", "uri", "page", "chunk_id", "score")}}
                              for s in ss.sources if s["source"] in cited])
        except Exception as e:  # provenance logging must never break the chat
            st.toast(f"Could not record citations: {e}")
        inventory.clear()  # pick up the new citation counts
        if searches:
            st.caption("Searched: " + " · ".join(f"“{q}”" for q in searches))
        render_sources(answer, turn_sources)
    ss.display.append({"role": "assistant", "text": answer, "sources": turn_sources, "searches": searches})
