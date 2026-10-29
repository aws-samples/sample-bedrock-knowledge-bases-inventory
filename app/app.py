# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Entry point: a weather demo over one Bedrock managed Knowledge Base (NOAA GSOD airport weather CSVs, 2020 onward).

The "Ask as" identity lives here, so every page retrieves as the same user (st.session_state.acl_user).
"""
import streamlit as st

from common import ACL_DEFAULT_USER, KB_ID

st.set_page_config(page_title="Weather KB demo", page_icon=":material/partly_cloudy_day:", layout="wide")

with st.sidebar:
    st.text_input("Ask as (user email for document ACLs)", ACL_DEFAULT_USER, key="acl_user",
                  help="Passed to every retrieval as userContext.userId and matched against acl/global-acl.json. "
                       "Not authenticated: this is ACL-aware filtering for the demo, not authorization.")
    if not st.session_state.acl_user.strip():
        st.warning("No user set: the ACL-enabled data source returns no documents.")
    st.caption(f"KB `{KB_ID}`")

st.navigation({
    "Retrieval": [
        st.Page("search_page.py", title="Basic search", icon=":material/search:", default=True),
        st.Page("agentic_page.py", title="Agentic search", icon=":material/account_tree:"),
        st.Page("instructions_page.py", title="Retrieval instructions", icon=":material/theater_comedy:"),
    ],
    "Access control": [
        st.Page("acl_page.py", title="ACL check", icon=":material/admin_panel_settings:"),
    ],
    "Apps": [
        st.Page("chat_page.py", title="Weather chat", icon=":material/chat:"),
        st.Page("inventory_page.py", title="Inventory", icon=":material/inventory_2:"),
    ],
}).run()
