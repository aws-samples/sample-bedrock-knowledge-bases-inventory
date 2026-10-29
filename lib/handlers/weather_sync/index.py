"""Mirror NOAA GSOD station CSVs from the AWS Open Data bucket into the KB's docs/ prefix, partitioned by year.

Every year from FromYear up to the latest one in the source bucket (discovered at run time, so a new year is
picked up without a redeploy) lands in <DestPrefix>noaa-gsod-<year>/<station>.csv. Server-side CopyObject, so
nothing is downloaded locally or committed to the repo. A file is copied only when the source ETag differs
from the one recorded on our copy, and CSVs for stations or years no longer in range are removed.
Two entry points:

- CloudFormation custom resource (deploy): syncs and returns a Fingerprint of the mirrored set.
  The stack's StartIngestion resource puts it in the job description, so changed data re-syncs the KB.
- EventBridge schedule: syncs and starts an ingestion job only if something changed.
"""
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

s3 = boto3.client('s3', config=Config(max_pool_connections=32))
bedrock = boto3.client('bedrock-agent')

SOURCE_BUCKET = os.environ['SOURCE_BUCKET']
DEST_BUCKET = os.environ['DEST_BUCKET']
PARTITION = re.compile(r'noaa-gsod-(\d{4})/')


def _head(bucket, key):
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response['Error']['Code'] in ('404', '403', 'NoSuchKey'):
            return None
        raise


def source_years(from_year):
    """Year folders (2020/, 2021/, ...) in the source bucket, from from_year on."""
    years = []
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=SOURCE_BUCKET, Delimiter='/'):
        for p in page.get('CommonPrefixes', []):
            y = p['Prefix'].rstrip('/')
            if y.isdigit() and int(y) >= int(from_year):
                years.append(y)
    return sorted(years)


def _sync_one(src_key, dst_key):
    """Returns (status, source ETag or None)."""
    src = _head(SOURCE_BUCKET, src_key)
    if src is None:
        return 'missing', None
    etag = src['ETag']
    dst = _head(DEST_BUCKET, dst_key)
    # Recorded source ETag first: a copy's ETag can differ (multipart source, encryption). Files uploaded
    # some other way (e.g. the old asset deployment) carry no record, but match on their own ETag.
    if dst and etag in (dst.get('Metadata', {}).get('source-etag'), dst['ETag']):
        return 'unchanged', etag
    s3.copy_object(Bucket=DEST_BUCKET, Key=dst_key, CopySource={'Bucket': SOURCE_BUCKET, 'Key': src_key},
                   ContentType='text/csv', Metadata={'source-etag': etag}, MetadataDirective='REPLACE')
    return 'copied', etag


def sync(from_year, stations, dest_prefix):
    years = source_years(from_year)
    pairs = [(f'{y}/{s}.csv', f'{dest_prefix}noaa-gsod-{y}/{s}.csv') for y in years for s in stations]
    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(lambda p: _sync_one(*p), pairs))

    copied, missing, etags = [], [], {}
    for (src_key, dst_key), (status, etag) in zip(pairs, outcomes):
        if status == 'missing':
            missing.append(src_key)
            continue
        if status == 'copied':
            copied.append(src_key)
        etags[dst_key] = etag

    # Prune only our own year partitions, and only stations or years no longer in range (a station briefly
    # unavailable upstream keeps its copy). Leaves *.metadata.json ACL overrides alone.
    wanted = {dst for _, dst in pairs}
    removed = []
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=DEST_BUCKET, Prefix=f'{dest_prefix}noaa-gsod-'):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('.csv') and PARTITION.search(key) and key not in wanted:
                s3.delete_object(Bucket=DEST_BUCKET, Key=key)
                removed.append(key)

    fingerprint = hashlib.sha256(json.dumps(sorted(etags.items())).encode()).hexdigest()[:16]
    result = {'Years': ','.join(years), 'Copied': len(copied), 'Unchanged': len(etags) - len(copied),
              'Missing': len(missing), 'Removed': len(removed), 'Fingerprint': fingerprint}
    print(json.dumps({**result, 'missingFiles': missing, 'copiedFiles': copied, 'removedKeys': removed}))
    return result


def _config(props):
    return props['FromYear'], json.loads(props['Stations']), props['DestPrefix']


def handler(event, _context):
    # CloudFormation custom resource (Provider framework: return value = response).
    if 'RequestType' in event:
        if event['RequestType'] == 'Delete':
            # Keep the mirrored files; the bucket is emptied with the stack anyway.
            return {'PhysicalResourceId': event['PhysicalResourceId']}
        props = event['ResourceProperties']
        result = sync(*_config(props))
        return {'PhysicalResourceId': f"weather-sync-{props['DestPrefix']}", 'Data': result}

    # Scheduled run.
    result = sync(*_config(event))
    if result['Copied'] or result['Removed']:
        try:
            job = bedrock.start_ingestion_job(
                knowledgeBaseId=os.environ['KB_ID'], dataSourceId=os.environ['DATA_SOURCE_ID'],
                description=f"weather-sync data-hash={result['Fingerprint']}")
            result['IngestionJobId'] = job['ingestionJob']['ingestionJobId']
        except bedrock.exceptions.ConflictException:
            # A job is already running; the next scheduled run picks up anything it missed.
            print('Ingestion already in progress; skipped')
    return result
