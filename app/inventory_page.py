# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Inventory page: source documents vs. what the KB has consumed, from APIs + KB logs + chat citations.

The Inventory tab is current state (one row per document); the Activity, Log events and SQL tabs are history.

Covers every data source of the KB. S3 sources are listed, so "in the source but not consumed" shows up;
SaaS sources (SharePoint, ServiceNow, ...) can't be listed, so their rows come from the KB API and logs only.
"""
import pandas as pd
import streamlit as st

from common import (FAILED_STATUSES, KB_ID, LOG_BUCKET, SOURCE_LISTING, data_sources, ingestion_jobs, inventory, query,
                    source_listings, user_query)

st.title("Bedrock Knowledge Base inventory")
st.caption(f"KB `{KB_ID}` / logs `s3://{LOG_BUCKET}`"
           + ("" if SOURCE_LISTING else " / built from the KB logs and Bedrock APIs only; source buckets aren't read"))

# Read-only: the task role can't start syncs. Ingestion runs from `cdk deploy` (weather or ACL changes) and the
# weather-sync schedule (only when the mirrored data changes).
if st.button("Refresh"):
    st.cache_data.clear()

inv = inventory()
ds = data_sources()
_, unlisted = source_listings()
with st.expander(f"Data sources ({len(ds)})"):
    st.dataframe(ds.assign(listed=~ds["data_source_id"].isin(unlisted) & ds["bucket"].notna()),
                 width="stretch", hide_index=True)
    if not SOURCE_LISTING:
        st.caption("Source listing is off (`SOURCE_LISTING`), so rows come from the KB logs and "
                   "ListKnowledgeBaseDocuments only: no file sizes and no NOT_CONSUMED / NOT_IN_SOURCE rows.")
    elif unlisted:
        st.caption("Unlisted sources (SaaS connectors, or S3 buckets the task role can't read) show only what the "
                   "KB API and logs know about: no NOT_CONSUMED / NOT_IN_SOURCE rows.")

# Current-state metrics. Chunk counts only exist in vector-KB logs; managed KBs don't log them.
metrics = [("Indexed in KB", int(inv["consumed"].sum()))]
if SOURCE_LISTING:
    metrics = [("Objects in listed sources", int(inv["in_source"].sum())), *metrics,
               ("Not consumed", int((inv["in_source"] & ~inv["consumed"]).sum())),
               ("Not in source", int((inv["listable"] & ~inv["in_source"]).sum()))]
else:
    metrics = [("Documents known (KB + logs)", len(inv)), *metrics,
               ("Deleted (logs)", int((inv["last_log_status"].eq("DELETED") & ~inv["consumed"]).sum()))]
metrics += [
    ("Failed / ignored (logs)", int(inv["last_log_status"].isin(FAILED_STATUSES + ["RESOURCE_IGNORED"]).sum())),
    ("Docs cited in chat", int((inv["times_cited"] > 0).sum())),
]
if inv["chunks_created"].notna().any():
    metrics.insert(4, ("Chunks indexed", int(inv["chunks_created"].fillna(0).sum())))
for col, (name, value) in zip(st.columns(len(metrics)), metrics):
    col.metric(name, value)

# Inventory = what is in the source and the KB now (one row per document). Activity = what syncs did over time.
tab_inv, tab_activity, tab_events, tab_sql = st.tabs(["Inventory", "Activity", "Log events", "SQL"])

with tab_inv:
    df = inv.copy()
    df["key"] = df["uri"].str.replace(r"^s3://[^/]+/", "", regex=True)
    # Without a source listing, a row with no KB/log status is unknown rather than "not consumed".
    df["state"] = "UNKNOWN"
    df.loc[df["in_source"], "state"] = "NOT_CONSUMED"
    df.loc[df["last_log_status"].isin(["RESOURCE_IGNORED"]), "state"] = "IGNORED"
    df.loc[df["kb_status"].eq("FAILED") | df["last_log_status"].isin(FAILED_STATUSES), "state"] = "FAILED"
    df.loc[df["kb_status"].isin(["PENDING", "STARTING", "IN_PROGRESS"]), "state"] = "IN_PROGRESS"
    df.loc[df["last_log_status"].eq("DELETED"), "state"] = "DELETED"
    df.loc[df["consumed"], "state"] = "INDEXED"
    df.loc[df["listable"] & ~df["in_source"], "state"] = "NOT_IN_SOURCE"  # KB/log only: since deleted from S3
    # Weather docs are partitioned as noaa-gsod-<year>/<station>.csv; other documents have no year or station.
    df["year"] = df["key"].str.extract(r"noaa-gsod-(\d{4})/", expand=False)
    # Station ID from the key, so the page never reads the documents themselves.
    df["station"] = df["key"].str.extract(r"noaa-gsod-\d{4}/([^/]+)\.csv$", expand=False)

    # ---- filters
    f1, f2, f3, f4, f5 = st.columns([3, 2, 2, 2, 2])
    search = f1.text_input("Search files", placeholder="substring of the key, e.g. noaa-gsod-2023 or 72530094846")
    states = f2.multiselect("State", sorted(df["state"].unique()))
    sources = f3.multiselect("Data source", sorted(df["data_source_name"].dropna().unique()))
    years = f4.multiselect("Year", sorted(df["year"].dropna().unique(), reverse=True))
    stations = f5.multiselect("Station", sorted(df["station"].dropna().unique()))
    if search:
        df = df[df["key"].str.contains(search, case=False, regex=False, na=False)]
    if states:
        df = df[df["state"].isin(states)]
    if sources:
        df = df[df["data_source_name"].isin(sources)]
    if years:
        df = df[df["year"].isin(years)]
    if stations:
        df = df[df["station"].isin(stations)]

    # ---- per-partition breakdown
    per_year = df.assign(year=df["year"].fillna("(none)")).groupby(["year", "state"]).size().unstack(fill_value=0)
    if not per_year.empty:
        st.subheader("Documents per year partition, by state")
        st.bar_chart(per_year, height=220)
        with st.expander("Per-year table"):
            st.dataframe(per_year.assign(total=per_year.sum(axis=1)).sort_index(ascending=False), width="stretch")

    # ---- paginated file list
    df = df.sort_values(["year", "key"], ascending=[False, True], na_position="last")
    p1, p2, p3 = st.columns([1, 1, 4])
    page_size = p1.selectbox("Rows per page", [50, 100, 250, 500, 1000], index=1)
    pages = max(1, -(-len(df) // page_size))
    page = p2.number_input("Page", min_value=1, max_value=pages, value=1, step=1)
    p3.caption(f"{len(df):,} files match; page {page} of {pages}")
    st.dataframe(
        df.iloc[(page - 1) * page_size: page * page_size][
            ["uri", "year", "station", "state", "data_source_name", "source_type"]
            + (["size_mb"] if SOURCE_LISTING else [])
            + ["last_modified", "kb_status", "kb_updated_at", "last_log_at", "jobs_seen", "times_cited",
               "last_cited_at", "last_reasons", "kb_status_reason", "source_uri"]],
        width="stretch", hide_index=True,
        # document_id (s3:// URI for S3) is what CheckIngestedDocumentAcl / GetIngestedDocumentAcl expect.
        column_config={"uri": st.column_config.TextColumn("document_id"),
                       "last_log_at": st.column_config.DatetimeColumn("last_processed_at"),
                       "jobs_seen": st.column_config.NumberColumn("jobs_processed_in")})
    st.download_button("Download filtered list (CSV)", df.to_csv(index=False), "bkb-inventory.csv", "text/csv")

with tab_activity:
    # Every log event counts, so each job's day shows up (a document re-processed by several jobs appears on
    # each of those days), unlike the inventory, which only knows each document's latest state.
    dated = query("""
        SELECT DISTINCT uri, CAST(ts AS DATE) AS day, status, ingestion_job_id
        FROM kb_logs WHERE uri IS NOT NULL AND status IS NOT NULL
    """)
    dated["day"] = pd.to_datetime(dated["day"]).dt.date
    if not dated.empty:
        a1, a2 = st.columns([3, 2])
        lo, hi = dated["day"].min(), dated["day"].max()
        picked = a1.date_input("Days (UTC)", (lo, hi), min_value=lo, max_value=hi)
        if isinstance(picked, (tuple, list)) and picked:
            lo, hi = picked[0], picked[-1]
        job_ids = a2.multiselect("Ingestion job", sorted(dated["ingestion_job_id"].dropna().unique()))
        dated = dated[dated["day"].between(lo, hi)]
        if job_ids:
            dated = dated[dated["ingestion_job_id"].isin(job_ids)]
        per_day = dated.groupby(["day", "status"])["uri"].nunique().unstack(fill_value=0).sort_index()
        if not per_day.empty:
            jobs_per_day = dated.groupby("day")["ingestion_job_id"].nunique()
            st.subheader("Documents processed per day (by logged status)")
            st.caption(f"{dated['ingestion_job_id'].nunique()} jobs on {len(jobs_per_day)} days in range. "
                       "A document re-processed by several jobs on one day counts once for that day.")
            # String index: one evenly sized bar per active day instead of slivers on a continuous time axis.
            st.bar_chart(per_day.set_axis(per_day.index.astype(str)), height=220)
            with st.expander("Per-day table"):
                st.dataframe(per_day.assign(total=per_day.sum(axis=1), jobs=jobs_per_day).sort_index(ascending=False),
                             width="stretch")

    st.subheader("From ListIngestionJobs")
    st.dataframe(ingestion_jobs(), width="stretch", hide_index=True)
    st.subheader("From logs (StartIngestionJob.StatusChanged)")
    milestones = query("""
        SELECT ts, data_source_id, ingestion_job_id, job_status,
               resource_statistics ->> 'number_of_resources_ingested' AS ingested,
               resource_statistics ->> 'number_of_resources_failed'   AS failed,
               resource_statistics ->> 'number_of_resources_deleted'  AS deleted
        FROM kb_logs WHERE job_status IS NOT NULL ORDER BY ts DESC
    """)
    # Vector KBs log per-job resource statistics; managed KBs don't (their counts are in the table above).
    st.dataframe(milestones.dropna(axis=1, how="all"), width="stretch", hide_index=True)

with tab_events:
    jobs = ingestion_jobs()
    picked_jobs: list[str] = []
    if not jobs.empty:
        st.caption("Click rows to show only those ingestion jobs' events (none selected = all jobs).")
        jobs_view = jobs[[c for c in ["ingestion_job_id", "data_source_id", "status", "started_at", "description",
                                      "numberOfDocumentsScanned", "numberOfNewDocumentsIndexed",
                                      "numberOfModifiedDocumentsIndexed", "numberOfDocumentsDeleted",
                                      "numberOfDocumentsFailed"] if c in jobs.columns]]
        sel = st.dataframe(jobs_view, width="stretch", hide_index=True, key="events_job_picker",
                           on_select="rerun", selection_mode="multi-row")
        # Job IDs come from ListIngestionJobs (10-char alphanumeric); the check keeps the inlined SQL literal safe.
        picked_jobs = [j for j in jobs_view.iloc[sel.selection.rows]["ingestion_job_id"]
                       if isinstance(j, str) and j.isalnum()]
    # Filter jobs in SQL so older jobs' events aren't cut off by the row limit.
    job_filter = ("WHERE ingestion_job_id IN (" + ", ".join(f"'{j}'" for j in picked_jobs) + ") "
                  if picked_jobs else "")
    ev = query("SELECT ts, level, event_type, data_source_id, ingestion_job_id, document_id, source_uri, "
               "hashed_document_id, crawl_action, crawl_status, index_status, sync_status, connector_status, status, "
               "status_reasons, chunks_created "
               f"FROM kb_logs {job_filter}ORDER BY ts DESC LIMIT 5000")  # nosec B608
    if not ev.empty:
        f1, f2, f3 = st.columns(3)
        for col, field, label in [(f1, "level", "Level"), (f2, "event_type", "Event type"), (f3, "status", "Status")]:
            vals = col.multiselect(label, sorted(ev[field].dropna().unique()), default=None, key=f"events_{field}")
            if vals:
                ev = ev[ev[field].isin(vals)]
    st.caption(f"{len(ev)} events" + (f" for job(s) {', '.join(picked_jobs)}" if picked_jobs else ""))
    st.dataframe(ev, width="stretch", hide_index=True)

with tab_sql:
    st.caption("Views: `kb_logs` (flattened), `kb_logs_raw` (as delivered), `kb_citations` (chat). DuckDB SQL.")
    sql = st.text_area("SQL", "SELECT status, count(*) AS n FROM kb_logs GROUP BY ALL ORDER BY n DESC", height=140)
    if st.button("Run"):
        try:
            st.dataframe(user_query(sql), width="stretch", hide_index=True)
        except Exception as e:  # surface DuckDB errors to the user
            st.error(str(e))
