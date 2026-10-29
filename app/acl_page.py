# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ACL check: can user X open document Y? One GetDocumentContent call with userContext per pair.

Unlike the console ACL test, this goes through the same access check as a real retrieval. Expected verdicts come from the stack's global ACL (DOCUMENT_ACLS).

  ALLOW      document returned for this user
  DENY       AccessDeniedException (also what a caller without GetDocumentContent gets, hence the control)
  NOT_FOUND  document id not in this data source
  ERROR      anything else
A run needs a positive control: at least one ALLOW-expected pair that returns ALLOW. Otherwise DENY verdicts
can't be told apart from a broken setup, and the run is INVALID.
"""
import time

import pandas as pd
import streamlit as st
from botocore.exceptions import ClientError

from common import (DATA_SOURCE_ID, DOCUMENT_ACLS, KB_ID, bedrock_agent_runtime, current_user, expected_access,
                    inventory, label)

UNKNOWN_USER = "nobody@example.com"


def check(doc_id: str, user: str, attempt: int = 0) -> tuple[str, str]:
    try:
        r = bedrock_agent_runtime.get_document_content(
            knowledgeBaseId=KB_ID, dataSourceId=DATA_SOURCE_ID, documentId=doc_id, outputFormat="EXTRACTED",
            userContext={"userId": user})
        return "ALLOW", f"{r.get('mimeType')} {r.get('documentContentLength')} bytes"
    except ClientError as e:
        code, msg = e.response["Error"]["Code"], e.response["Error"].get("Message", "")
        if code == "AccessDeniedException":
            return "DENY", msg
        if code == "ResourceNotFoundException":
            return "NOT_FOUND", msg
        if code == "ThrottlingException" and attempt < 5:
            time.sleep(2 ** attempt)
            return check(doc_id, user, attempt + 1)
        return "ERROR", f"{code}: {msg}"


st.title("ACL check")
st.caption("Per document and user: `GetDocumentContent` with `userContext`, compared with what the stack's "
           "global ACL file predicts. Deny overrides allow; documents with no ACL entry are never ingested.")

with st.expander("Configured ACL (acl/global-acl.json)", expanded=True):
    st.dataframe(pd.DataFrame([{"prefix": a["prefix"], "allow": ", ".join(a.get("allow", [])),
                                "deny": ", ".join(a.get("deny", []))} for a in DOCUMENT_ACLS]),
                 hide_index=True, width="stretch")

inv = inventory()
docs = inv[(inv["data_source_id"] == DATA_SOURCE_ID) & inv["kb_status"].eq("INDEXED")]["uri"].dropna().tolist()
if not docs:
    st.warning("No INDEXED documents in the stack's data source yet.")
    st.stop()

known = list(dict.fromkeys([current_user(), *(u for a in DOCUMENT_ACLS for u in a.get("allow", []) + a.get("deny", [])),
                            UNKNOWN_USER]))
users = st.multiselect("Users", [u for u in known if u], default=[u for u in known if u], accept_new_options=True,
                       help="Include one user who certainly has access: the positive control.")
picked = st.multiselect("Documents", docs, default=docs[:3], format_func=label)

if st.button("Run checks", type="primary", disabled=not (users and picked)):
    rows = []
    with st.spinner(f"Checking {len(users) * len(picked)} pairs..."):
        for doc in picked:
            for user in users:
                verdict, detail = check(doc, user)
                expected = expected_access(doc, user)
                rows.append({"station": label(doc), "user": user, "expected": expected, "verdict": verdict,
                             "match": verdict == expected, "detail": detail, "document_id": doc})
    st.session_state.acl_last = pd.DataFrame(rows)

df = st.session_state.get("acl_last")
if df is not None and not df.empty:
    controls_ok = ((df["expected"] == "ALLOW") & (df["verdict"] == "ALLOW")).any()
    mismatches = int((~df["match"]).sum())
    if not controls_ok:
        st.error("**RUN INVALID**: no ALLOW-expected pair returned ALLOW, so the DENY rows can't be trusted. "
                 "Check the task role's GetDocumentContent permission, the document ids, and that the allowed "
                 "email is in the ACL exactly.")
    elif mismatches:
        st.warning(f"{mismatches} mismatch(es) against the configured ACL.")
    else:
        st.success("All pairs match the configured ACL.")

    matrix = df.pivot(index="station", columns="user", values="verdict")
    st.dataframe(matrix.style.map(lambda v: f"color: {'green' if v == 'ALLOW' else 'red' if v == 'DENY' else 'orange'}"),
                 width="stretch")
    st.dataframe(df.style.apply(lambda r: ["" if r["match"] else "background-color: rgba(255,0,0,.15)"] * len(r), axis=1),
                 hide_index=True, width="stretch")
    st.download_button("Download results.csv", df.to_csv(index=False), "results.csv", "text/csv")
