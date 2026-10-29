# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared clients, retrieval helpers, DuckDB views and inventory data for all pages.

Connector-agnostic: every data source of the KB is covered (S3, SharePoint, ServiceNow, Confluence, ...).
Rows are keyed by the KB `document_id` (an s3:// URI for S3, the connector's ID otherwise).

Sources:
  - S3 listing of each S3 data source  -> what *should* be in the KB (SaaS sources have no listing)
  - ListKnowledgeBaseDocuments API     -> what the KB says it has right now
  - KB application logs on S3 (DuckDB) -> per-document history across ingestion jobs
  - chat citation events on S3 (DuckDB) -> which documents answers were actually grounded in
"""
import csv
import datetime as dt
import io
import json
import os
import re
import time
import uuid

import boto3
from botocore.exceptions import ClientError
import duckdb
import pandas as pd
import streamlit as st

REGION = os.environ["AWS_REGION"]
KB_ID = os.environ["KB_ID"]
# The stack's own S3 data source. Fallback when the task role can't List/GetDataSource; other data sources
# of the KB are discovered at runtime.
DATA_SOURCE_ID = os.environ["DATA_SOURCE_ID"]
DATA_BUCKET = os.environ["DATA_BUCKET"]
DATA_PREFIX = os.environ.get("DATA_PREFIX", "")
LOG_BUCKET = os.environ["LOG_BUCKET"]
FAILED_STATUSES = ["FAILED", "FAILURE", "EMBEDDING_FAILED", "INDEXING_FAILED", "DELETION_FAILED", "METADATA_UPDATE_FAILED"]
# Only this KB's logs: vended logs land under AWSLogs/<acct>/bedrock/knowledgebases/<region>/<kbId>/yyyy/MM/dd/HH/,
# and a replaced KB leaves its old logs behind. Override for local dev, e.g. LOG_GLOB=./sample-logs/**/*.log.gz
LOG_GLOB = os.environ.get(
    "LOG_GLOB", f"s3://{LOG_BUCKET}/AWSLogs/*/bedrock/knowledgebases/{REGION}/{KB_ID}/**/*.log.gz")
# Chat citation events, written by the chat page. Hive-style dt= partitions, one JSONL object per answer.
CITATION_PREFIX = "app/citations/"
CITATION_GLOB = os.environ.get("CITATION_GLOB", f"s3://{LOG_BUCKET}/{CITATION_PREFIX}**/*.jsonl")
ACL_DEFAULT_USER = os.environ.get("ACL_DEFAULT_USER", "")
CHAT_MODELS = [m for m in os.environ.get("CHAT_MODELS", "us.anthropic.claude-sonnet-5-5").split(",") if m]
# [{prefix, allow: [emails], deny: [emails]}], the stack's global ACL file in relative form (expected verdicts).
DOCUMENT_ACLS = json.loads(os.environ.get("DOCUMENT_ACLS") or "[]")
# [{key, label, id, queries}], the retrieval instructions deployed by the stack.
RETRIEVAL_INSTRUCTIONS = json.loads(os.environ.get("RETRIEVAL_INSTRUCTIONS") or "[]")
# Managed-KB system metadata key that names the data source of a result.
DS_META_KEY = "_data_source_id"

session = boto3.Session(region_name=REGION)
s3 = session.client("s3")
bedrock_agent = session.client("bedrock-agent")
bedrock_agent_runtime = session.client("bedrock-agent-runtime")
bedrock_runtime = session.client("bedrock-runtime")


def to_s3_uri(uri: str | None) -> str | None:
    """S3 https://<bucket>.s3[.<region>].amazonaws.com/<key> URLs -> s3://<bucket>/<key>. Other URLs are unchanged."""
    if uri and uri.startswith("https://"):
        host, _, key = uri[len("https://"):].partition("/")
        if ".s3." in host and host.endswith(".amazonaws.com"):
            return f"s3://{host.split('.s3.', 1)[0]}/{key}"
    return uri


def location_uri(location: dict) -> str | None:
    """URI from any Retrieve / document_location shape: {type, s3Location: {uri}}, {type, sharePointLocation: {url}}, ..."""
    for v in (location or {}).values():
        if isinstance(v, dict):
            return v.get("uri") or v.get("url") or v.get("id")
    return None


def to_document_id(uri: str | None) -> str | None:
    """Map a Retrieve location (the document's source_uri) to the KB document_id the inventory is keyed by."""
    if not uri:
        return uri
    inv = inventory()
    ids = dict(zip(inv["source_uri"], inv["uri"]))
    return ids.get(uri) or to_s3_uri(uri)


# ----------------------------------------------------------------- DuckDB
def duck() -> duckdb.DuckDBPyConnection:
    """New connection per query run, with fresh task-role creds (they rotate)."""
    con = duckdb.connect()
    con.execute("LOAD httpfs")
    con.execute("SET TimeZone = 'UTC'")
    c = session.get_credentials().get_frozen_credentials()
    con.execute(
        "CREATE OR REPLACE SECRET s3 (TYPE s3, KEY_ID ?, SECRET ?, SESSION_TOKEN ?, REGION ?)",
        [c.access_key, c.secret_key, c.token or "", REGION],
    )
    # `event` is typed JSON because resource-level and job-level events have different shapes.
    _json_view(con, "kb_logs_raw", LOG_GLOB,
               {"event_timestamp": "BIGINT", "event_type": "VARCHAR", "level": "VARCHAR",
                "event_version": "VARCHAR", "event": "JSON"})
    con.execute("""
        CREATE OR REPLACE VIEW kb_logs AS
        WITH e AS (
            SELECT *,
                   event ->> '$.connector_document_status.Status'  AS connector_status,
                   event ->> '$.sync_status.Status'                 AS sync_status,
                   -- Vector KBs nest the location per connector type (s3_location.uri, web_location.url, ...).
                   list_concat(json_extract_string(event, '$.document_location.*.uri'),
                               json_extract_string(event, '$.document_location.*.url'),
                               json_extract_string(event, '$.document_location.*.id'))[1] AS location_uri,
                   event_type = 'StartIngestionJob.ResourceStatusChanged' AS is_document_event
            FROM kb_logs_raw
        )
        SELECT
            to_timestamp(event_timestamp / 1000)                         AS ts,
            event_type,
            level,
            event ->> 'ingestion_job_id'                                 AS ingestion_job_id,
            event ->> 'data_source_id'                                   AS data_source_id,
            -- Managed KBs log document_id for every connector (an s3:// URI for S3); vector KBs log
            -- document_location. Only s3:/ URIs are normalized.
            regexp_replace(COALESCE(event ->> 'document_id', location_uri, event ->> 'source_uri'),
                           '^s3:/+', 's3://')                            AS uri,
            event ->> 'document_id'                                      AS document_id,
            -- What Retrieve returns as the location (https:// for S3, the page URL for SaaS connectors).
            regexp_replace(COALESCE(event ->> 'source_uri', event ->> '$.metadata._source_uri', location_uri),
                           '^s3:/+', 's3://')                            AS source_uri,
            COALESCE(event ->> '$.document_location.type', event ->> 'content_type') AS content_type,
            event ->> 'hashed_document_id'                               AS hashed_document_id,
            event ->> 'document_title'                                   AS document_title,
            event ->> 'crawl_action'                                     AS crawl_action,
            event ->> '$.crawl_status.Status'                            AS crawl_status,
            event ->> '$.index_status.Status'                            AS index_status,
            sync_status,
            connector_status,
            -- Managed KBs report per-stage SUCCESS/FAILED (connector_document_status is the consolidated one);
            -- map them onto the vector-KB document statuses.
            COALESCE(event ->> 'status', CASE
                WHEN COALESCE(connector_status, sync_status) = 'SUCCESS'
                    THEN CASE WHEN event ->> 'crawl_action' = 'DELETE' THEN 'DELETED' ELSE 'INDEXED' END
                WHEN event ->> 'error_message' IS NOT NULL OR COALESCE(connector_status, sync_status) = 'FAILED'
                    OR (is_document_event AND level = 'ERROR') THEN 'FAILED'
                ELSE COALESCE(connector_status, sync_status) END)       AS status,
            -- Some document events carry only a `message`; that's a reason, not a job status.
            COALESCE(event ->> 'ingestion_job_status',
                     CASE WHEN NOT is_document_event THEN event ->> 'message' END) AS job_status,
            COALESCE(NULLIF(CAST(event -> 'status_reasons' AS VARCHAR), '[]'), event ->> 'error_message',
                     CASE WHEN is_document_event THEN event ->> 'message' END) AS status_reasons,
            TRY_CAST(event ->> '$.chunk_statistics.created' AS INTEGER)  AS chunks_created,
            TRY_CAST(event ->> '$.chunk_statistics.ignored' AS INTEGER)  AS chunks_ignored,
            TRY_CAST(event ->> '$.chunk_statistics.failed_to_create' AS INTEGER) AS chunks_failed,
            event -> 'resource_statistics'                               AS resource_statistics,
            event,
            filename
        FROM e
    """)
    _json_view(con, "kb_citations", CITATION_GLOB,
               {"ts": "TIMESTAMPTZ", "session_id": "VARCHAR", "model": "VARCHAR", "user_id": "VARCHAR",
                "question": "VARCHAR", "source": "VARCHAR", "uri": "VARCHAR", "page": "INTEGER", "chunk_id": "VARCHAR", "score": "DOUBLE"})
    # The SQL tab runs user-typed SQL on this connection. Sandbox it: only the log and citation prefixes stay
    # readable, so no local files (/proc/self/environ), no HTTP (the task-credentials endpoint), no other S3
    # paths, no extension installs, and the settings can't be changed back.
    con.execute("SET allowed_directories = ?", [[_glob_root(LOG_GLOB), _glob_root(CITATION_GLOB)]])
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


def _glob_root(glob: str) -> str:
    """Directory prefix of a glob, ending in '/', e.g. s3://b/AWSLogs/*/x/**/*.gz -> s3://b/AWSLogs/."""
    root = re.split(r"[*?\[{]", glob, maxsplit=1)[0].rsplit("/", 1)[0] + "/"
    return root if "://" in root else os.path.abspath(root) + "/"


def _json_view(con: duckdb.DuckDBPyConnection, name: str, glob: str, columns: dict) -> None:
    """View over NDJSON files; an empty view with the same columns if nothing has been written yet."""
    cols = ", ".join(f"{k}: '{v}'" for k, v in columns.items())
    # Identifiers, glob and column types come from code and stack config, never from user input.
    try:
        # nosemgrep
        con.execute(f"""CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_json('{glob}',
            format = 'newline_delimited', columns = {{{cols}}}, filename = true, ignore_errors = true)""")  # nosec B608
    except duckdb.IOException as e:
        if "No files found" not in str(e):
            raise
        typed = ", ".join(f"NULL::{v} AS {k}" for k, v in columns.items())
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT {typed}, NULL::VARCHAR AS filename WHERE false")  # nosec B608 # nosemgrep


@st.cache_data(ttl=60, show_spinner="Reading KB logs from S3 with DuckDB...")
def query(sql: str) -> pd.DataFrame:
    con = duck()
    try:
        return con.execute(sql).df()
    finally:
        con.close()


# allowed_directories also permits writes, and the task role can put objects under app/citations/, so the
# SQL tab must not run COPY ... TO, EXPORT DATABASE or ATTACH there. Only read-only statements are accepted,
# classified by DuckDB's own parser.
_READ_ONLY = {duckdb.StatementType.SELECT, duckdb.StatementType.EXPLAIN}


def user_query(sql: str) -> pd.DataFrame:
    """Run SQL typed in the UI: SELECT, WITH ... SELECT, DESCRIBE, SUMMARIZE or EXPLAIN only."""
    con = duckdb.connect()
    try:
        statements = con.extract_statements(sql)
    finally:
        con.close()
    if not statements:
        raise ValueError("No SQL statement to run.")
    rejected = sorted({s.type.name for s in statements if s.type not in _READ_ONLY})
    if rejected:
        raise ValueError(f"Only read-only queries (SELECT, EXPLAIN) are allowed; got {', '.join(rejected)}.")
    return query(sql)


# ---------------------------------------------------------- AWS listings
def _connector(cfg: dict) -> tuple[str | None, str | None, list[str]]:
    """(connector type, S3 bucket, S3 prefixes) from a data source configuration, managed or vector KB."""
    if cfg.get("type") == "MANAGED_KNOWLEDGE_BASE_CONNECTOR":
        p = cfg.get("managedKnowledgeBaseConnectorConfiguration", {}).get("connectorParameters") or {}
        p = json.loads(p) if isinstance(p, str) else p
        if p.get("type") == "S3":
            return ("S3", p.get("connectionConfiguration", {}).get("bucketName"),
                    p.get("filterConfiguration", {}).get("inclusionPrefixes") or [""])
        return p.get("type"), None, []
    if cfg.get("type") == "S3":
        c = cfg.get("s3Configuration", {})
        return "S3", c.get("bucketArn", "").rpartition(":")[2] or None, c.get("inclusionPrefixes") or [""]
    return cfg.get("type"), None, []


def _sync_settings(cfg: dict) -> tuple[str, str]:
    """(sync schedule, deletion protection) of a managed connector, as display strings.

    With a schedule the KB syncs on its own, not only on `cdk deploy`. With deletion protection a sync that would
    delete more than the threshold skips its delete phase, so documents removed at the source stay INDEXED.
    """
    m = cfg.get("managedKnowledgeBaseConnectorConfiguration") or {}
    sched = m.get("syncSchedule") or {}
    if "daily" in sched:
        schedule = "daily"
    elif "weekly" in sched:
        schedule = f"weekly ({sched['weekly'].get('dayOfWeek')})"
    elif "monthly" in sched:
        mo = sched["monthly"].get("dayOfMonth", {})
        schedule = "monthly (last day)" if "lastDayOfMonth" in mo else f"monthly (day {mo.get('dayNumber')})"
    else:
        schedule = "on demand"
    dp = m.get("deletionProtectionConfiguration") or {}
    status = dp.get("deletionProtectionStatus")
    protection = (f"ENABLED ({dp.get('deletionProtectionThreshold', 15)}%)" if status == "ENABLED"
                  else status or "not set")
    return schedule, protection


@st.cache_data(ttl=300)
def data_sources() -> pd.DataFrame:
    """Every data source of the KB. Falls back to the stack's own S3 source if the role can't list them."""
    cols = ["data_source_id", "data_source_name", "status", "source_type", "bucket", "prefixes", "sync_schedule",
            "deletion_protection"]
    try:
        ids = [(d["dataSourceId"], d.get("name"), d.get("status"))
               for page in bedrock_agent.get_paginator("list_data_sources").paginate(knowledgeBaseId=KB_ID)
               for d in page.get("dataSourceSummaries", [])]
        rows = []
        for ds_id, name, status in ids:
            cfg = bedrock_agent.get_data_source(knowledgeBaseId=KB_ID, dataSourceId=ds_id)["dataSource"]
            cfg = cfg.get("dataSourceConfiguration", {})
            rows.append([ds_id, name, status, *_connector(cfg), *_sync_settings(cfg)])
    except ClientError as e:
        if e.response["Error"]["Code"] != "AccessDeniedException":
            raise
        rows = [[DATA_SOURCE_ID, "docs", None, "S3", DATA_BUCKET, [DATA_PREFIX], None, None]]
    return pd.DataFrame(rows, columns=cols)


@st.cache_data(ttl=60)
def source_listings() -> tuple[pd.DataFrame, list[str]]:
    """List the objects of every S3 data source. Returns (objects, sources that could not be listed).

    Only S3 can be listed with the task role; SaaS connectors (SharePoint, ServiceNow, ...) have no listing,
    so for them the inventory is KB API + logs only.
    """
    rows, unlisted = [], []
    for ds in data_sources().itertuples():
        if not ds.bucket:
            unlisted.append(ds.data_source_id)
            continue
        try:
            for prefix in ds.prefixes:
                for page in s3.get_paginator("list_objects_v2").paginate(Bucket=ds.bucket, Prefix=prefix):
                    for o in page.get("Contents", []):
                        if not o["Key"].endswith("/"):
                            rows.append({"uri": f"s3://{ds.bucket}/{o['Key']}", "data_source_id": ds.data_source_id,
                                         "size_mb": round(o["Size"] / 1e6, 2), "last_modified": o["LastModified"]})
        except ClientError:  # e.g. a bucket the task role can't read
            unlisted.append(ds.data_source_id)
    return pd.DataFrame(rows, columns=["uri", "data_source_id", "size_mb", "last_modified"]), unlisted


def _identifier_uri(identifier: dict) -> str | None:
    """DocumentIdentifier is {dataSourceType, s3: {uri}} or {dataSourceType, custom: {id}}."""
    return next((v.get("uri") or v.get("id") for v in identifier.values() if isinstance(v, dict)), None)


@st.cache_data(ttl=60)
def kb_documents() -> pd.DataFrame:
    rows = []
    for ds_id in data_sources()["data_source_id"]:
        token = None
        while True:
            kw = {"knowledgeBaseId": KB_ID, "dataSourceId": ds_id}
            if token:
                kw["nextToken"] = token
            r = bedrock_agent.list_knowledge_base_documents(**kw)
            for d in r.get("documentDetails", []):
                rows.append({"uri": _identifier_uri(d.get("identifier", {})), "data_source_id": ds_id,
                             "kb_status": d.get("status"), "kb_status_reason": d.get("statusReason"),
                             "kb_updated_at": d.get("updatedAt")})
            token = r.get("nextToken")
            if not token:
                break
    return pd.DataFrame(rows, columns=["uri", "data_source_id", "kb_status", "kb_status_reason", "kb_updated_at"])


@st.cache_data(ttl=30)
def ingestion_jobs() -> pd.DataFrame:
    rows = []
    for ds_id in data_sources()["data_source_id"]:
        r = bedrock_agent.list_ingestion_jobs(knowledgeBaseId=KB_ID, dataSourceId=ds_id,
                                              sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
                                              maxResults=50)
        rows += [{"ingestion_job_id": j["ingestionJobId"], "data_source_id": ds_id, "status": j["status"],
                  "started_at": j.get("startedAt"), "updated_at": j.get("updatedAt"),
                  "description": j.get("description"), **j.get("statistics", {})}
                 for j in r.get("ingestionJobSummaries", [])]
    return pd.DataFrame(rows)


# ------------------------------------------------------------- inventory
@st.cache_data(ttl=60, show_spinner="Building inventory...")
def inventory() -> pd.DataFrame:
    latest = query("""
        SELECT uri, arg_max(status, ts) AS last_log_status, max(ts) AS last_log_at,
               arg_max(data_source_id, ts) AS data_source_id,
               arg_max(source_uri, ts) FILTER (WHERE source_uri IS NOT NULL) AS source_uri,
               arg_max(document_title, ts) FILTER (WHERE document_title IS NOT NULL) AS document_title,
               arg_max(hashed_document_id, ts) FILTER (WHERE hashed_document_id IS NOT NULL) AS hashed_document_id,
               arg_max(status_reasons, ts) FILTER (WHERE status_reasons IS NOT NULL) AS last_reasons,
               max(chunks_created) FILTER (WHERE status IN ('INDEXED', 'PARTIALLY_INDEXED')) AS chunks_created,
               count(DISTINCT ingestion_job_id) AS jobs_seen
        FROM kb_logs WHERE uri IS NOT NULL GROUP BY uri
    """)
    if latest.empty:
        latest = pd.DataFrame(columns=["uri", "last_log_status", "last_log_at", "data_source_id", "source_uri",
                                       "document_title", "hashed_document_id", "last_reasons", "chunks_created",
                                       "jobs_seen"])

    # Outer joins: a doc can be in S3 but never ingested, or only in logs (e.g. ignored, or since deleted from S3).
    listed, unlisted = source_listings()
    inv = listed.merge(kb_documents(), on="uri", how="outer", suffixes=("", "_kb"))
    inv = inv.merge(latest, on="uri", how="outer", suffixes=("", "_log"))
    inv["data_source_id"] = inv["data_source_id"].fillna(inv.pop("data_source_id_kb")).fillna(
        inv.pop("data_source_id_log"))
    ds = data_sources()
    inv = inv.merge(ds[["data_source_id", "data_source_name", "source_type"]], on="data_source_id", how="left")
    inv["consumed"] = inv["kb_status"].eq("INDEXED")
    inv["in_source"] = inv["size_mb"].notna()
    # Listable = we know the full source contents (S3 sources the task role can read).
    inv["listable"] = inv["data_source_id"].isin(set(ds["data_source_id"][ds["bucket"].notna()]) - set(unlisted))
    cites = query("""
        SELECT uri, count(*) AS times_cited, max(ts) AS last_cited_at, count(DISTINCT page) AS pages_cited
        FROM kb_citations WHERE uri IS NOT NULL GROUP BY uri
    """)
    inv = inv.merge(cites, on="uri", how="left")
    inv["times_cited"] = inv["times_cited"].fillna(0).astype(int)
    return inv


def record_citations(rows: list[dict]) -> None:
    """Append one JSONL object per answer under app/citations/dt=YYYY-MM-DD/."""
    if not rows:
        return
    now = dt.datetime.now(dt.timezone.utc)
    key = f"{CITATION_PREFIX}dt={now:%Y-%m-%d}/{now:%H%M%S}-{uuid.uuid4().hex[:8]}.jsonl"
    body = "\n".join(json.dumps({"ts": now.isoformat(), **r}, default=str) for r in rows) + "\n"
    s3.put_object(Bucket=LOG_BUCKET, Key=key, Body=body.encode(), ContentType="application/x-ndjson")


# ------------------------------------------------------------ identity
def current_user() -> str:
    """The sidebar "Ask as" email (app.py), passed as userContext.userId. Not authenticated: demo only."""
    return (st.session_state.get("acl_user") or "").strip()


def expected_access(document_id: str, user: str) -> str:
    """ALLOW / DENY that the stack's global ACL predicts for a document and user. Deny overrides allow.

    Doesn't know about per-document <file>.metadata.json overrides, which win over the global file.
    """
    key = document_id.removeprefix(f"s3://{DATA_BUCKET}/")
    matches = [a for a in DOCUMENT_ACLS if key.startswith(a["prefix"])]
    if any(user in a.get("deny", []) for a in matches):
        return "DENY"
    return "ALLOW" if any(user in a.get("allow", []) for a in matches) else "DENY"


# --------------------------------------------------------------- weather
@st.cache_data(ttl=3600, show_spinner="Reading station names...")
def station_names() -> dict[str, str]:
    """document_id -> "station NAME (year)". The NAME comes from the first data row of one CSV per station
    (a 2 KB range read); the year from its noaa-gsod-<year>/ partition."""
    names, by_station = {}, {}
    listed, _ = source_listings()
    for uri in listed["uri"]:
        if uri.endswith(".csv"):
            by_station.setdefault(uri.rsplit("/", 1)[-1], []).append(uri)
    for uris in by_station.values():
        bucket, _, key = uris[0][len("s3://"):].partition("/")
        try:
            head = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-2047")["Body"].read().decode("utf-8", "replace")
            row = next(csv.DictReader(io.StringIO(head)), None)
        except ClientError:
            continue
        if not (row and row.get("NAME")):
            continue
        # "CHICAGO OHARE INTERNATIONAL AIRPORT, IL US" -> "Chicago Ohare International Airport, IL US"
        place, sep, region = row["NAME"].rpartition(",")
        name = f"{place.title()}{sep}{region}" if sep else row["NAME"].title()
        for uri in uris:
            year = re.search(r"noaa-gsod-(\d{4})/", uri)
            names[uri] = f"{name} ({year.group(1)})" if year else name
    return names


def label(document_id: str | None) -> str:
    """Station name (or object key) for a document id."""
    if not document_id:
        return "?"
    return station_names().get(document_id) or document_id.removeprefix(f"s3://{DATA_BUCKET}/")


# ------------------------------------------------------------- retrieval
def retrieve(query: str, k: int, user: str | None) -> list[dict]:
    """Plain Retrieve (hybrid search + managed reranking). user=None sends no userContext (the ACL control)."""
    kw = {"knowledgeBaseId": KB_ID, "retrievalQuery": {"text": query},
          "retrievalConfiguration": {"managedSearchConfiguration": {"numberOfResults": k}}}
    if user:
        kw["userContext"] = {"userId": user}
    rows = []
    for i, res in enumerate(bedrock_agent_runtime.retrieve(**kw).get("retrievalResults", []), 1):
        md = res.get("metadata", {})
        uri = to_document_id(location_uri(res.get("location", {})))
        rows.append({"rank": i, "score": round(res.get("score", 0.0), 3), "station": label(uri),
                     "document_id": uri, "chunk_id": md.get("_chunk_id"), "text": res.get("content", {}).get("text", "")})
    return rows


def agentic_retrieve(query: str, user: str | None, *, max_results: int = 10, iterations: int = 3,
                     answer: bool = True, instruction_id: str | None = None, version: str | None = None,
                     on_trace=None, on_text=None) -> dict:
    """One AgenticRetrieveStream call. on_trace(step dict) and on_text(answer so far) are called while streaming.

    Returns {results, answer, citations, steps, applied, seconds}. results are the deduplicated chunks from all
    iterations; citation references index into them.
    """
    req = {
        "messages": [{"role": "user", "content": {"text": query}}],
        "retrievers": [{"configuration": {"knowledgeBase": {
            "knowledgeBaseId": KB_ID, "retrievalOverrides": {"maxNumberOfResults": max_results}}}}],
        # MANAGED: the service picks the planning model and the reranker.
        "agenticRetrieveConfiguration": {"foundationModelType": "MANAGED", "rerankingModelType": "MANAGED",
                                         "maxAgentIteration": iterations},
        "generateResponse": answer,
    }
    if user:
        req["userContext"] = {"userId": user}
    if instruction_id:
        req["retrievalInstructionConfiguration"] = {"retrievalInstructionId": instruction_id,
                                                    **({"version": str(version)} if version else {})}
    t0 = time.time()
    out = {"results": [], "answer": "", "citations": [], "steps": [], "applied": None}
    for event in bedrock_agent_runtime.agentic_retrieve_stream(**req)["stream"]:
        if "traceEvent" in event:
            a = event["traceEvent"].get("attributes", {})
            if a.get("appliedInstruction") and out["applied"] is None:
                out["applied"] = a["appliedInstruction"]   # on the first Planning event
            acts = a.get("actions", [])
            # Full-document expansion actions also carry an empty "retrieve": {}, so key on inputQuery.
            step = {"step": a.get("step", "?"), "status": a.get("status", ""), "message": a.get("message", ""),
                    "queries": [x["retrieve"]["inputQuery"].get("text", "") for x in acts
                                if "inputQuery" in x.get("retrieve", {})],
                    "expanded": [x["fullDocumentExpansion"]["documentId"] for x in acts if "fullDocumentExpansion" in x],
                    "failures": [f.get("message") for f in a.get("failures", []) if f.get("message")]}
            out["steps"].append(step)
            if on_trace:
                on_trace(step)
        elif "responseEvent" in event:
            out["answer"] += event["responseEvent"].get("text", "")
            if on_text:
                on_text(out["answer"])
        elif "result" in event:
            r = event["result"]
            out["results"] = r.get("results", [])
            g = r.get("generatedResponse") or {}
            out["answer"] = g.get("answer", out["answer"])
            out["citations"] = g.get("citations", [])
        else:
            name = next(iter(event))
            raise RuntimeError(f"stream error {name}: {event[name]}")
    out["seconds"] = round(time.time() - t0, 1)
    return out


def agentic_result_rows(results: list[dict]) -> pd.DataFrame:
    rows = []
    for i, r in enumerate(results, 1):
        md = r.get("metadata") or {}
        uri = to_document_id(md.get("_source_uri") or location_uri(r.get("location", {})))
        rows.append({"#": i, "station": label(uri), "document_id": uri,
                     "score": round(r["score"], 3) if r.get("score") is not None else None,
                     "text": (r.get("content") or {}).get("text", "")})
    df = pd.DataFrame(rows, columns=["#", "station", "document_id", "score", "text"])
    return df.drop(columns="score") if df["score"].isna().all() else df   # agentic results may carry no score


def with_citation_marks(answer: str, citations: list[dict]) -> str:
    """Insert [n] after each cited span; n is the 1-based index into the results table."""
    marks = sorted(((c["endIndex"], sorted({ref["resultIndex"] + 1 for ref in c.get("references", [])}))
                    for c in citations if c.get("endIndex") is not None), reverse=True)
    for end, refs in marks:
        if refs:
            answer = answer[:end] + "".join(f" [{n}]" for n in refs) + answer[end:]
    return answer
