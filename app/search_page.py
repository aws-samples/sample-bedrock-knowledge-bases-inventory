"""Basic search: one Retrieve call (hybrid search + managed reranking), with and without the user.

The run without userContext is the ACL control: an ACL-enabled data source must return 0 results for it.
"""
import pandas as pd
import streamlit as st
from botocore.exceptions import ClientError

from common import current_user, retrieve

EXAMPLES = [
    "Hottest day at Chicago O'Hare in 2024",
    "Heavy snow at Denver International Airport",
    "Fog at Heathrow in December 2024",
    "Strongest wind gusts in Tokyo",
]

user = current_user()
st.title("Basic search")
st.caption("One `Retrieve` call: hybrid search plus managed reranking, no model in the loop. "
           f"Runs as `{user or 'nobody'}`, then without a user as the ACL control.")

with st.sidebar:
    st.subheader("Search settings")
    k = st.slider("Results", 1, 25, 10)
    control = st.toggle("Run the no-user control", True,
                        help="Retrieve without userContext. ACL-enabled data sources must return 0 results.")

ex = st.pills("Examples", EXAMPLES, label_visibility="collapsed")
query = st.text_input("Query", ex or "", placeholder="e.g. hottest day at Phoenix in July 2024")

if query:
    try:
        with st.spinner("Retrieving..."):
            rows = retrieve(query, k, user) if user else []
            ctrl = retrieve(query, k, None) if control else None
    except ClientError as e:
        st.error(f"{e.response['Error']['Code']}: {e.response['Error']['Message']}")
        st.stop()

    c1, c2, c3 = st.columns(3)
    c1.metric(f"Results as {user or 'nobody'}", len(rows))
    c2.metric("Distinct stations", len({r["document_id"] for r in rows}))
    if ctrl is not None:
        c3.metric("Control (no userContext)", len(ctrl), help="Must be 0 for an ACL-enabled data source.")
        if ctrl:
            st.error("The control returned results: the data source isn't filtering by ACL.")
    if not user:
        st.warning("Set **Ask as** in the sidebar; without a user the KB returns nothing.")
    elif not rows:
        st.info("No results. If the control is 0 too, check that this email is ALLOWed in the ACL "
                "(see the ACL check page).")

    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"text": st.column_config.TextColumn("chunk", width="large"),
                                    "score": st.column_config.ProgressColumn("score", min_value=0, max_value=1,
                                                                             format="%.3f")})
        st.caption("Each chunk is a slice of one station's daily CSV rows. Retrieve returns chunks, not answers: "
                   "questions like \"which month was wettest\" need many chunks, which is where agentic search helps.")
