// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as path from 'path';
import * as crypto from 'crypto';
import * as cdk from 'aws-cdk-lib/core';
import { Construct, IConstruct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecsPatterns from 'aws-cdk-lib/aws-ecs-patterns';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as s3Assets from 'aws-cdk-lib/aws-s3-assets';
import * as lambdaNode from 'aws-cdk-lib/aws-lambda-nodejs';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import { RETRIEVAL_INSTRUCTIONS, RetrievalInstructionPreset } from './retrieval-instructions';
import { DEFAULT_WEATHER_FROM_YEAR, NOAA_GSOD_BUCKET, WEATHER_STATIONS } from './weather-stations';

/** One entry in the global ACL file. `prefix` is a key prefix (folder) or object key in the data bucket. */
export interface DocumentAcl {
  readonly prefix: string;
  /** User emails allowed to retrieve documents under the prefix. */
  readonly allow: string[];
  /** User emails denied. Deny overrides allow. */
  readonly deny?: string[];
}

export interface BkbInventoryStackProps extends cdk.StackProps {
  /**
   * Bedrock embedding model ID for a CUSTOM-embedding managed KB (1024 dims, float32).
   * Default: service-managed embedding, which also enables the managed reranker.
   */
  readonly embeddingModelId?: string;
  /** Bedrock inference profile IDs offered in the chat page; the first is the default. */
  readonly chatModelIds?: string[];
  /**
   * Document-level ACLs written to the global ACL file. Documents with no matching entry are NOT ingested.
   * Default: `docs/` allowed for the comma-separated emails in the `aclAllow` context (and denied for `aclDeny`).
   */
  readonly documentAcls?: DocumentAcl[];
  /** Retrieval instructions (agentic-retrieval personas) to deploy. Default: the weather presets. */
  readonly retrievalInstructions?: RetrievalInstructionPreset[];
  /**
   * First NOAA GSOD year to mirror. Every year from it up to the latest one in the source bucket is mirrored,
   * one partition per year (`docs/weather/noaa-gsod-<year>/`). Default: 2020 (or the `weatherFromYear` context).
   */
  readonly weatherFromYear?: string;
  /** NOAA GSOD station ids to mirror. Default: the 46 airports in `lib/weather-stations.ts`. */
  readonly weatherStations?: string[];
  /**
   * How often the weather-sync Lambda re-checks the source and, only if something changed, starts an ingestion job.
   * Default: daily. Pass `null` to sync only on deploy.
   */
  readonly weatherSyncSchedule?: events.Schedule | null;
}

const APP_DIR = path.join(__dirname, '..', 'app');
const DATA_PREFIX = 'docs/';
const ACL_KEY = 'acl/global-acl.json';

const emails = (v: unknown): string[] => String(v ?? '').split(',').map(s => s.trim()).filter(Boolean);

function hashAcls(acls: DocumentAcl[]): string {
  return crypto.createHash('sha256').update(JSON.stringify(acls)).digest('hex').slice(0, 16);
}

export class BkbInventoryStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: BkbInventoryStackProps = {}) {
    super(scope, id, props);

    const embeddingModelArn = props.embeddingModelId
      ? `arn:${this.partition}:bedrock:${this.region}::foundation-model/${props.embeddingModelId}`
      : undefined;
    const documentAcls = props.documentAcls ?? [{
      prefix: DATA_PREFIX,
      allow: emails(this.node.tryGetContext('aclAllow')),
      deny: emails(this.node.tryGetContext('aclDeny')),
    }];
    if (!documentAcls.some(a => a.allow.length)) {
      cdk.Annotations.of(this).addWarning(
        'No ACL allows anyone (set -c aclAllow=user@example.com or documentAcls). Nothing will be ingested.');
    }
    const chatModelIds = props.chatModelIds ?? [
      'us.anthropic.claude-sonnet-5-5', 'us.anthropic.claude-haiku-5-5', 'us.anthropic.claude-opus-5-5',
    ];

    // ---------------------------------------------------------------- buckets
    // Audit trail: S3 server access logs of the two buckets below, ALB access logs and VPC flow logs.
    // SSE-S3 because ALB log delivery doesn't support KMS.
    const accessLogsBucket = new s3.Bucket(this, 'AccessLogsBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      lifecycleRules: [{ expiration: cdk.Duration.days(90) }],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const dataBucket = new s3.Bucket(this, 'DataBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      serverAccessLogsBucket: accessLogsBucket,
      serverAccessLogsPrefix: 's3/data-bucket/',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const logBucket = new s3.Bucket(this, 'KbLogBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      serverAccessLogsBucket: accessLogsBucket,
      serverAccessLogsPrefix: 's3/kb-log-bucket/',
      // Citation events hold each chat question and the "Ask as" e-mail address; don't keep them forever.
      // KB logs under AWSLogs/ are kept: they are the ingestion history the inventory is built from.
      lifecycleRules: [{ id: 'ExpireChatCitations', prefix: 'app/citations/', expiration: cdk.Duration.days(90) }],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Vended-log delivery writes as delivery.logs.amazonaws.com. Grant it explicitly
    // rather than relying on the auto-created policy (which CFN-managed policies would overwrite).
    const deliverySourceArnPattern = `arn:${this.partition}:logs:${this.region}:${this.account}:delivery-source:*`;
    logBucket.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'AWSLogDeliveryWrite',
      principals: [new iam.ServicePrincipal('delivery.logs.amazonaws.com')],
      actions: ['s3:PutObject'],
      resources: [logBucket.arnForObjects('*')],
      conditions: {
        StringEquals: { 'aws:SourceAccount': this.account, 's3:x-amz-acl': 'bucket-owner-full-control' },
        ArnLike: { 'aws:SourceArn': deliverySourceArnPattern },
      },
    }));

    // --------------------------------------------- weather docs (S3 → S3)
    // Mirrored server-side from the NOAA GSOD Open Data bucket, so the CSVs never touch the repo or the
    // deploying machine. One partition per year from weatherFromYear to the latest year upstream (found at
    // run time, so the schedule picks up new years). Runs at deploy (custom resource) and on a schedule
    // (ingests only on change).
    const weatherFromYear = String(props.weatherFromYear ?? this.node.tryGetContext('weatherFromYear') ?? DEFAULT_WEATHER_FROM_YEAR);
    const weatherStations = props.weatherStations ?? WEATHER_STATIONS;
    const weatherPrefix = `${DATA_PREFIX}weather/`;
    const weatherConfig = { FromYear: weatherFromYear, Stations: JSON.stringify(weatherStations), DestPrefix: weatherPrefix };
    const weatherSync = new lambda.Function(this, 'WeatherSync', {
      runtime: lambda.Runtime.PYTHON_3_14,
      architecture: lambda.Architecture.ARM_64,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, 'handlers', 'weather_sync')),
      timeout: cdk.Duration.minutes(10),
      memorySize: 512,
      environment: { SOURCE_BUCKET: NOAA_GSOD_BUCKET, DEST_BUCKET: dataBucket.bucketName },
    });
    // The source is a public bucket in another account: its bucket policy allows List/Get for everyone,
    // but cross-account access also needs them in our identity policy. ListBucket finds the year folders.
    weatherSync.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket'],
      resources: [`arn:${this.partition}:s3:::${NOAA_GSOD_BUCKET}`],
    }));
    weatherSync.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject'],
      resources: [`arn:${this.partition}:s3:::${NOAA_GSOD_BUCKET}/*`],
    }));
    // Write includes DeleteObject (pruning dropped stations and years).
    dataBucket.grantReadWrite(weatherSync, `${weatherPrefix}*`);
    const weatherSyncProvider = new cr.Provider(this, 'WeatherSyncProvider', { onEventHandler: weatherSync });
    const docsSync = new cdk.CustomResource(this, 'WeatherDocsSync', {
      serviceToken: weatherSyncProvider.serviceToken,
      resourceType: 'Custom::NoaaGsodSync',
      properties: weatherConfig,
    });

    // ------------------------------------------------------- global ACL file
    // Must live in the data bucket; it sits outside docs/ so it is never ingested itself.
    const aclEntries = documentAcls.map(a => ({
      keyPrefix: `s3://${dataBucket.bucketName}/${a.prefix}`,
      aclEntries: [
        ...a.allow.map(Name => ({ Name, Type: 'USER', Access: 'ALLOW' })),
        ...(a.deny ?? []).map(Name => ({ Name, Type: 'USER', Access: 'DENY' })),
      ],
    }));
    const aclDeployment = new s3deploy.BucketDeployment(this, 'DeployAcl', {
      sources: [s3deploy.Source.jsonData(path.basename(ACL_KEY), aclEntries)],
      destinationBucket: dataBucket,
      destinationKeyPrefix: path.dirname(ACL_KEY) + '/',
    });

    // -------------------------------------------------------- knowledge base
    // MANAGED KB: Bedrock owns the vector store. Required for ACL-aware retrieval on S3 sources.
    const kbRole = new iam.Role(this, 'KbRole', {
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: { 'aws:SourceArn': `arn:${this.partition}:bedrock:${this.region}:${this.account}:knowledge-base/*` },
        },
      }),
      // Inline so the permissions exist before the KB is created (CreateKnowledgeBase validates them).
      inlinePolicies: {
        kb: new iam.PolicyDocument({
          statements: [
            ...(embeddingModelArn
              ? [new iam.PolicyStatement({ actions: ['bedrock:InvokeModel'], resources: [embeddingModelArn] })]
              : []),
            new iam.PolicyStatement({ actions: ['s3:ListBucket'], resources: [dataBucket.bucketArn] }),
            // The documents and the ACL file; nothing else in the bucket.
            new iam.PolicyStatement({
              actions: ['s3:GetObject'],
              resources: [dataBucket.arnForObjects(`${DATA_PREFIX}*`), dataBucket.arnForObjects(ACL_KEY)],
            }),
          ],
        }),
      },
    });

    const kb = new bedrock.CfnKnowledgeBase(this, 'KnowledgeBase', {
      // KB names are unique per account and CFN creates the replacement before deleting the old KB,
      // so change this name whenever a change forces KB replacement.
      name: `${this.stackName}-managed-kb`,
      roleArn: kbRole.roleArn,
      knowledgeBaseConfiguration: {
        type: 'MANAGED',
        managedKnowledgeBaseConfiguration: embeddingModelArn
          ? {
            embeddingModelType: 'CUSTOM',
            embeddingModelArn,
            embeddingModelConfiguration: {
              bedrockEmbeddingModelConfiguration: { dimensions: 1024, embeddingDataType: 'FLOAT32' },
            },
          }
          : { embeddingModelType: 'MANAGED' },
      },
    });
    kb.node.addDependency(kbRole);

    const dataSource = new bedrock.CfnDataSource(this, 'S3DataSource', {
      knowledgeBaseId: kb.attrKnowledgeBaseId,
      name: 'docs',
      // RETAIN: the managed vector store goes away with the KB anyway.
      dataDeletionPolicy: 'RETAIN',
      dataSourceConfiguration: {
        type: 'MANAGED_KNOWLEDGE_BASE_CONNECTOR',
        managedKnowledgeBaseConnectorConfiguration: {
          // aclEnabled can't be changed after creation; flipping it means replacing the data source.
          connectorParameters: {
            type: 'S3',
            version: '1',
            aclEnabled: true,
            connectionConfiguration: { bucketName: dataBucket.bucketName, bucketOwnerAccountId: this.account },
            filterConfiguration: { inclusionPrefixes: [DATA_PREFIX] },
            aclConfiguration: { globalAccessControlListS3Uri: dataBucket.s3UrlForObject(ACL_KEY) },
          },
        },
      },
    });

    // ------------------------------------------------------ KB app logging
    const kbLogGroup = new logs.LogGroup(this, 'KbLogGroup', {
      // /aws/vendedlogs/ keeps us under the CWL resource-policy size limit.
      logGroupName: `/aws/vendedlogs/bedrock/knowledge-base/${this.stackName}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const cwlPolicy = new logs.CfnResourcePolicy(this, 'KbLogGroupPolicy', {
      policyName: `${this.stackName}-kb-vended-logs`,
      policyDocument: JSON.stringify({
        Version: '2012-10-17',
        Statement: [{
          Sid: 'AWSLogDeliveryWrite',
          Effect: 'Allow',
          Principal: { Service: 'delivery.logs.amazonaws.com' },
          Action: ['logs:CreateLogStream', 'logs:PutLogEvents'],
          Resource: `${kbLogGroup.logGroupArn}:log-stream:*`,
          Condition: {
            StringEquals: { 'aws:SourceAccount': this.account },
            ArnLike: { 'aws:SourceArn': deliverySourceArnPattern },
          },
        }],
      }),
    });

    const deliverySource = new logs.CfnDeliverySource(this, 'KbDeliverySource', {
      // A delivery source can't be repointed at a new resource; rename it together with the KB.
      name: `${this.stackName}-managed-kb-src`,
      logType: 'APPLICATION_LOGS',
      resourceArn: kb.attrKnowledgeBaseArn,
    });

    const s3Destination = new logs.CfnDeliveryDestination(this, 'KbS3Destination', {
      name: `${this.stackName}-kb-s3`,
      destinationResourceArn: logBucket.bucketArn,
      outputFormat: 'json',
    });
    const cwlDestination = new logs.CfnDeliveryDestination(this, 'KbCwlDestination', {
      name: `${this.stackName}-kb-cwl`,
      destinationResourceArn: kbLogGroup.logGroupArn,
      outputFormat: 'json',
    });

    const s3Delivery = new logs.CfnDelivery(this, 'KbS3Delivery', {
      deliverySourceName: deliverySource.name,
      deliveryDestinationArn: s3Destination.attrArn,
      // Default key layout ends in {region}/{yyyy}/{MM}/{dd}/{HH}/. The bucket is
      // dedicated to these logs, so the UI just globs the whole bucket.
      // (KB logs to S3 support plain/json/w3c only; no parquet.)
    });
    s3Delivery.node.addDependency(deliverySource);
    s3Delivery.node.addDependency(logBucket.policy!);

    const cwlDelivery = new logs.CfnDelivery(this, 'KbCwlDelivery', {
      deliverySourceName: deliverySource.name,
      deliveryDestinationArn: cwlDestination.attrArn,
    });
    cwlDelivery.node.addDependency(deliverySource);
    cwlDelivery.node.addDependency(cwlPolicy);
    // Concurrent CreateDelivery calls on one source can conflict; serialize them.
    cwlDelivery.node.addDependency(s3Delivery);

    // -------------------------------------------- kick off ingestion (sync)
    // Runs after the weather docs are mirrored, the ACL file is uploaded and both log deliveries exist, so the first
    // sync is fully logged. Description carries a fingerprint of the mirrored weather files and a hash of
    // the ACLs: changing either re-syncs.
    const ingestion = new cr.AwsCustomResource(this, 'StartIngestion', {
      installLatestAwsSdk: false,
      onUpdate: {
        service: 'bedrock-agent',
        action: 'StartIngestionJob',
        parameters: {
          knowledgeBaseId: kb.attrKnowledgeBaseId,
          dataSourceId: dataSource.attrDataSourceId,
          description: `cdk deploy data-hash=${docsSync.getAttString('Fingerprint')} acl-hash=${hashAcls(documentAcls)}`,
        },
        physicalResourceId: cr.PhysicalResourceId.fromResponse('ingestionJob.ingestionJobId'),
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({ actions: ['bedrock:StartIngestionJob'], resources: [kb.attrKnowledgeBaseArn] }),
      ]),
    });
    ingestion.node.addDependency(docsSync, aclDeployment, s3Delivery, cwlDelivery);

    const weatherSyncSchedule = props.weatherSyncSchedule === undefined
      ? events.Schedule.rate(cdk.Duration.days(1))
      : props.weatherSyncSchedule;
    if (weatherSyncSchedule) {
      weatherSync.addEnvironment('KB_ID', kb.attrKnowledgeBaseId);
      weatherSync.addEnvironment('DATA_SOURCE_ID', dataSource.attrDataSourceId);
      weatherSync.addToRolePolicy(new iam.PolicyStatement({
        actions: ['bedrock:StartIngestionJob'],
        resources: [kb.attrKnowledgeBaseArn],
      }));
      new events.Rule(this, 'WeatherSyncSchedule', {
        schedule: weatherSyncSchedule,
        targets: [new targets.LambdaFunction(weatherSync, { event: events.RuleTargetInput.fromObject(weatherConfig) })],
      });
    }

    // ------------------------------------------------ retrieval instructions
    // No CloudFormation resource type yet: a custom resource creates/updates each one and waits for the
    // content review (a rejected review fails the deploy with the reasons). The UI only reads them.
    const riHandlerProps: lambdaNode.NodejsFunctionProps = {
      entry: path.join(__dirname, 'handlers', 'retrieval-instruction.ts'),
      runtime: lambda.Runtime.NODEJS_24_X,
      timeout: cdk.Duration.minutes(10),
      // Bundle the SDK: the runtime's built-in one may predate retrieval instructions.
      bundling: { externalModules: [], minify: true },
    };
    const riOnEvent = new lambdaNode.NodejsFunction(this, 'RetrievalInstructionOnEvent', { ...riHandlerProps, handler: 'onEvent' });
    const riIsComplete = new lambdaNode.NodejsFunction(this, 'RetrievalInstructionIsComplete', { ...riHandlerProps, handler: 'isComplete' });
    const riArnPattern = `arn:${this.partition}:bedrock:${this.region}:${this.account}:retrieval-instruction/*`;
    riOnEvent.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:CreateRetrievalInstruction', 'bedrock:GetRetrievalInstruction', 'bedrock:UpdateRetrievalInstruction',
        'bedrock:DeleteRetrievalInstruction', 'bedrock:ListRetrievalInstructionVersions'],
      resources: [riArnPattern],
    }));
    riOnEvent.addToRolePolicy(new iam.PolicyStatement({ actions: ['bedrock:ListRetrievalInstructions'], resources: ['*'] }));
    riIsComplete.addToRolePolicy(new iam.PolicyStatement({ actions: ['bedrock:GetRetrievalInstruction'], resources: [riArnPattern] }));
    const riProvider = new cr.Provider(this, 'RetrievalInstructionProvider', {
      onEventHandler: riOnEvent,
      isCompleteHandler: riIsComplete,
      queryInterval: cdk.Duration.seconds(10),
      totalTimeout: cdk.Duration.minutes(20),
      disableWaiterStateMachineLogging: false,
      waiterStateMachineLogOptions: {
        destination: new logs.LogGroup(this, 'RetrievalInstructionWaiterLogs', {
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        level: sfn.LogLevel.ALL,
      },
    });
    const instructions = (props.retrievalInstructions ?? RETRIEVAL_INSTRUCTIONS).map(preset => {
      const res = new cdk.CustomResource(this, `RetrievalInstruction-${preset.key}`, {
        serviceToken: riProvider.serviceToken,
        resourceType: 'Custom::BedrockRetrievalInstruction',
        properties: { Name: `${this.stackName}-${preset.key}`, Description: preset.description, Instruction: preset.instruction },
      });
      return { preset, id: res.getAttString('RetrievalInstructionId'), arn: res.getAttString('Arn') };
    });

    // ------------------------------------- UI image: built in CodeBuild, not locally
    // The app/ dir ships as a zip asset; a native ARM CodeBuild project builds and pushes
    // it to ECR during deploy, so no local Docker or container runtime is needed.
    const imageRepo = new ecr.Repository(this, 'UiRepo', {
      imageScanOnPush: true,
      emptyOnDelete: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const buildContext = new s3Assets.Asset(this, 'UiSource', { path: APP_DIR });
    // Tag = content hash, so the image (and task definition) only change when app/ changes.
    const imageTag = buildContext.assetHash.slice(0, 32);

    const imageBuild = new codebuild.Project(this, 'UiImageBuild', {
      source: codebuild.Source.s3({ bucket: buildContext.bucket, path: buildContext.s3ObjectKey }),
      environment: {
        buildImage: codebuild.LinuxArmBuildImage.fromCodeBuildImageId('aws/codebuild/amazonlinux-aarch64-standard:3.0'),
        computeType: codebuild.ComputeType.SMALL,
        privileged: true,
      },
      environmentVariables: {
        ECR_REPO_URI: { value: imageRepo.repositoryUri },
        IMAGE_TAG: { value: imageTag },
        ACCOUNT_ID: { value: this.account },
        REGION: { value: this.region },
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          pre_build: {
            commands: [
              'aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"',
            ],
          },
          build: { commands: ['docker build -t "$ECR_REPO_URI:$IMAGE_TAG" .'] },
          post_build: {
            commands: [
              // post_build runs even if build failed; don't push a missing image.
              '[ "$CODEBUILD_BUILD_SUCCEEDING" = "1" ] || exit 1',
              'docker image inspect "$ECR_REPO_URI:$IMAGE_TAG" --format "Architecture: {{.Architecture}} OS: {{.Os}}"',
              'docker push "$ECR_REPO_URI:$IMAGE_TAG"',
            ],
          },
        },
      }),
    });
    buildContext.grantRead(imageBuild);
    imageRepo.grantPullPush(imageBuild);

    // Start the build at deploy time and wait for SUCCEEDED, so a broken build fails the stack.
    const buildTrigger = new lambda.Function(this, 'UiImageBuildTrigger', {
      runtime: lambda.Runtime.NODEJS_24_X,
      handler: 'index.handler',
      timeout: cdk.Duration.minutes(15),
      environment: { PROJECT_NAME: imageBuild.projectName },
      code: lambda.Code.fromInline(`
const { CodeBuildClient, StartBuildCommand, BatchGetBuildsCommand } = require('@aws-sdk/client-codebuild');
const cb = new CodeBuildClient({});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

exports.handler = async (event) => {
  if (event.RequestType === 'Delete') {
    return { PhysicalResourceId: event.PhysicalResourceId };
  }
  const started = await cb.send(new StartBuildCommand({ projectName: process.env.PROJECT_NAME }));
  const buildId = started.build.id;
  console.log('Started CodeBuild build', buildId);
  for (;;) {
    await sleep(15000);
    const got = await cb.send(new BatchGetBuildsCommand({ ids: [buildId] }));
    const status = got.builds[0].buildStatus;
    console.log('Build status:', status);
    if (status === 'SUCCEEDED') {
      return { PhysicalResourceId: buildId, Data: { BuildId: buildId } };
    }
    if (status !== 'IN_PROGRESS') {
      throw new Error('CodeBuild build ' + buildId + ' finished with status ' + status);
    }
  }
};
`),
    });
    buildTrigger.addToRolePolicy(new iam.PolicyStatement({
      actions: ['codebuild:StartBuild', 'codebuild:BatchGetBuilds'],
      resources: [imageBuild.projectArn],
    }));
    const buildProvider = new cr.Provider(this, 'UiImageBuildProvider', { onEventHandler: buildTrigger });
    const imageBuilt = new cdk.CustomResource(this, 'UiImageBuilt', {
      serviceToken: buildProvider.serviceToken,
      properties: { ImageTag: imageTag },
    });
    imageBuilt.node.addDependency(imageBuild);

    // ------------------------------------------------ Streamlit on Fargate
    // Nothing internet-facing: internal ALB + tasks in private subnets with no public IP.
    // Access is via SSM port forwarding through the task (ECS Exec); see scripts/tunnel.sh.
    // One NAT is cheaper than the ~6 interface endpoints (ECR x2, logs, ssmmessages, bedrock-agent) it replaces.
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC },
        { name: 'private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      ],
      gatewayEndpoints: { S3: { service: ec2.GatewayVpcEndpointAwsService.S3 } },
      flowLogs: {
        s3: { destination: ec2.FlowLogDestination.toS3(accessLogsBucket, 'vpc-flow-logs/') },
      },
    });
    const cluster = new ecs.Cluster(this, 'Cluster', { vpc, containerInsightsV2: ecs.ContainerInsights.ENABLED });
    // Egress is HTTPS only: Bedrock, S3 (gateway endpoint), ECR, CloudWatch Logs and SSM are all on 443.
    // DNS and time sync use the VPC resolver and link-local addresses, which security groups don't filter.
    const uiServiceSg = new ec2.SecurityGroup(this, 'UiServiceSg', {
      vpc,
      allowAllOutbound: false,
      description: 'Streamlit UI tasks: ingress from the ALB, egress HTTPS and the tunnel hop to the ALB',
    });
    uiServiceSg.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'HTTPS to AWS service endpoints');

    const ui = new ecsPatterns.ApplicationLoadBalancedFargateService(this, 'Ui', {
      cluster,
      securityGroups: [uiServiceSg],
      cpu: 512,
      memoryLimitMiB: 1024,
      desiredCount: 1,
      minHealthyPercent: 100,
      circuitBreaker: { rollback: true },
      enableExecuteCommand: true,
      assignPublicIp: false,
      taskSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      publicLoadBalancer: false,
      openListener: false,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
      taskImageOptions: {
        image: ecs.ContainerImage.fromEcrRepository(imageRepo, imageTag),
        containerPort: 8501,
        environment: {
          AWS_REGION: this.region,
          KB_ID: kb.attrKnowledgeBaseId,
          DATA_SOURCE_ID: dataSource.attrDataSourceId,
          DATA_BUCKET: dataBucket.bucketName,
          DATA_PREFIX,
          // Pre-fills the chat page's ACL identity. Not authentication: anyone with the tunnel can change it.
          ACL_DEFAULT_USER: documentAcls.find(a => a.allow.length)?.allow[0] ?? '',
          LOG_BUCKET: logBucket.bucketName,
          LOG_GROUP: kbLogGroup.logGroupName,
          CHAT_MODELS: chatModelIds.join(','),
          // Expected verdicts for the ACL check page (the task role can't read acl/ itself).
          DOCUMENT_ACLS: JSON.stringify(documentAcls),
          RETRIEVAL_INSTRUCTIONS: this.toJsonString(instructions.map(({ preset, id }) => ({
            key: preset.key, label: preset.label, id, queries: preset.queries,
          }))),
        },
        logDriver: ecs.LogDrivers.awsLogs({
          streamPrefix: 'ui',
          logGroup: new logs.LogGroup(this, 'UiLogGroup', {
            retention: logs.RetentionDays.TWO_WEEKS,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
          }),
        }),
      },
    });
    // Tunnelled traffic reaches the ALB from the task's ENI, i.e. from inside the VPC.
    ui.loadBalancer.connections.allowFrom(ec2.Peer.ipv4(vpc.vpcCidrBlock), ec2.Port.tcp(80), 'Streamlit UI (in-VPC only)');
    // The SSM port-forwarding session runs from the task to the ALB, so the task needs egress to it on 80.
    ui.service.connections.allowTo(ui.loadBalancer, ec2.Port.tcp(80), 'SSM tunnel hop to the internal ALB');
    // The service pulls the CodeBuild-pushed image, so wait for the build.
    ui.service.node.addDependency(imageBuilt);
    ui.targetGroup.configureHealthCheck({ path: '/_stcore/health', healthyHttpCodes: '200' });
    ui.targetGroup.setAttribute('deregistration_delay.timeout_seconds', '10');
    ui.loadBalancer.setAttribute('routing.http.drop_invalid_header_fields.enabled', 'true');
    ui.loadBalancer.logAccessLogs(accessLogsBucket, 'alb');

    const taskRole = ui.taskDefinition.taskRole;
    // Least privilege: the UI only reads. No sync (StartIngestionJob) and no access to acl/; ingestion runs from deploy.
    dataBucket.grantRead(taskRole, `${DATA_PREFIX}*`);
    // KB logs and chat citations only, the two prefixes the DuckDB views read.
    logBucket.grantRead(taskRole, 'AWSLogs/*');
    logBucket.grantRead(taskRole, 'app/citations/*');
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      // List/GetDataSource: the inventory covers every data source of the KB (S3, SharePoint, ServiceNow, ...).
      // GetDataSource returns connector config incl. secret ARNs, never the secret values.
      actions: ['bedrock:ListKnowledgeBaseDocuments', 'bedrock:ListIngestionJobs', 'bedrock:ListDataSources',
        'bedrock:GetDataSource'],
      resources: [kb.attrKnowledgeBaseArn],
    }));
    // Chat page: KB Retrieve + Converse on the cross-Region inference profiles. A profile routes to the
    // underlying foundation model in any of its Regions, so the model ARN is Region-wildcarded.
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:Retrieve'],
      resources: [kb.attrKnowledgeBaseArn],
    }));
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [
        ...chatModelIds.map(id => `arn:${this.partition}:bedrock:${this.region}:${this.account}:inference-profile/${id}`),
        ...chatModelIds.map(id => `arn:${this.partition}:bedrock:*::foundation-model/${id.replace(/^[a-z]+\./, '')}*`),
      ],
    }));
    // Agentic search and retrieval-instruction pages (AgenticRetrieveStream), and the ACL check page
    // (GetDocumentContent with userContext; agentic retrieval also calls it for full-document expansion).
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:GetDocumentContent'],
      resources: [kb.attrKnowledgeBaseArn],
    }));
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      // AgenticRetrieveStream has no resource-level scoping; the service-managed planning model is invoked
      // with the caller's credentials and isn't named up front, hence '*' (as in the Bedrock user guide).
      actions: ['bedrock:AgenticRetrieveStream', 'bedrock:InvokeModelWithResponseStream'],
      resources: ['*'],
    }));
    if (instructions.length) {
      taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        // Applying an instruction needs Get on it; versions have no ARN of their own.
        actions: ['bedrock:GetRetrievalInstruction'],
        resources: instructions.map(i => i.arn),
      }));
    }
    // Chat citation events (read back by DuckDB for the inventory's "times cited").
    logBucket.grantPut(taskRole, 'app/citations/*');

    // ------------------------------------------------- cdk-nag acknowledgments
    // Every AwsSolutions finding left after hardening, with the reason it is accepted (bin/ runs the pack on
    // synth). cdk-nag v3 matches finding IDs exactly, so they are built from the resolved names below.
    const ack = (scope: IConstruct, reason: string, ...ids: string[]) =>
      cdk.Validations.of(scope).acknowledge(...ids.map(id => ({ id, reason })));
    // A value is a literal when account and Region are known, otherwise an Fn::Sub that cdk-nag
    // renders with <AWS::X> placeholders (e.g. the asset bucket when only the Region is known).
    const pseudo = (v: string, name: string) => {
      const r = this.resolve(v);
      if (typeof r === 'string') return r;
      const sub = r?.['Fn::Sub'];
      return typeof sub === 'string' ? sub.replace(/\$\{(AWS::\w+)\}/g, '<$1>') : `<AWS::${name}>`;
    };
    const [P, R, A] = [pseudo(this.partition, 'Partition'), pseudo(this.region, 'Region'), pseudo(this.account, 'AccountId')];
    const lid = (c: IConstruct) => this.getLogicalId(c.node.defaultChild as cdk.CfnElement);
    const iam5 = (finding: string) => `AwsSolutions-IAM5[${finding}]`;
    const s3Read = ['GetBucket*', 'GetObject*', 'List*'].map(a => iam5(`Action::s3:${a}`));
    const s3Write = ['Abort*', 'DeleteObject*'].map(a => iam5(`Action::s3:${a}`));
    const assetBucket = pseudo(buildContext.bucket.bucketName, 'AssetBucket');
    const anyResource = iam5('Resource::*');

    ack(this, 'AWSLambdaBasicExecutionRole only grants writing the function\'s own CloudWatch logs.',
      'AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]');
    for (const [provider, handlers] of [[weatherSyncProvider, [weatherSync]], [riProvider, [riOnEvent, riIsComplete]],
      [buildProvider, [buildTrigger]]] as [Construct, lambda.Function[]][]) {
      const fns = [...handlers.map(lid), ...provider.node.findAll()
        .filter((c): c is lambda.CfnFunction => c instanceof lambda.CfnFunction).map(c => this.getLogicalId(c))];
      ack(provider, 'CDK provider framework: invokes any version of its own handler functions.',
        ...fns.map(f => iam5(`Resource::<${f}.Arn>:*`)));
    }
    ack(weatherSync, 'Mirrors the public NOAA GSOD Open Data bucket into docs/weather/ only, pruning removed files.',
      ...s3Read, ...s3Write, iam5(`Resource::<${lid(dataBucket)}.Arn>/${weatherPrefix}*`),
      iam5(`Resource::arn:${P}:s3:::${NOAA_GSOD_BUCKET}/*`));
    ack(kbRole, 'The KB ingests every document under docs/ (its inclusion prefix).',
      iam5(`Resource::<${lid(dataBucket)}.Arn>/${DATA_PREFIX}*`));
    const riNames = iam5(`Resource::arn:${P}:bedrock:${R}:${A}:retrieval-instruction/*`);
    ack(riOnEvent, 'Retrieval instruction IDs are service-generated and unknown up front; ListRetrievalInstructions '
      + '(finding one by name) has no resource-level scoping.', riNames, anyResource);
    ack(riIsComplete, 'Retrieval instruction IDs are service-generated and unknown up front.', riNames);
    ack(imageBuild, 'CDK-generated CodeBuild role: read of the build-context asset, its own log group and report groups, '
      + 'and ecr:GetAuthorizationToken, which has no resource-level scoping.',
      ...s3Read, anyResource, iam5(`Resource::arn:${P}:s3:::${assetBucket}/*`),
      iam5(`Resource::arn:${P}:codebuild:${R}:${A}:report-group/<${lid(imageBuild)}>-*`),
      iam5(`Resource::arn:${P}:logs:${R}:${A}:log-group:/aws/codebuild/<${lid(imageBuild)}>:*`));
    ack(imageBuild, 'The project produces no artifacts (the image goes to ECR); the source asset is SSE-S3 encrypted.',
      'AwsSolutions-CB4');
    ack(ui.taskDefinition, 'Task role: read of docs/ and of the KB-log and citation prefixes, put of citations; '
      + 'cross-Region inference profiles route to the model in any of their Regions; AgenticRetrieveStream and its '
      + 'service-picked planning model have no resource-level scoping. Execution role: ecr:GetAuthorizationToken has '
      + 'no resource-level scoping.',
      ...s3Read, iam5('Action::s3:Abort*'), anyResource,
      iam5(`Resource::<${lid(dataBucket)}.Arn>/${DATA_PREFIX}*`),
      iam5(`Resource::<${lid(logBucket)}.Arn>/AWSLogs/*`),
      iam5(`Resource::<${lid(logBucket)}.Arn>/app/citations/*`),
      ...chatModelIds.map(id => iam5(`Resource::arn:${P}:bedrock:*::foundation-model/${id.replace(/^[a-z]+\./, '')}*`)));
    ack(ui.taskDefinition, 'The environment holds resource IDs and config only, no secrets.', 'AwsSolutions-ECS2');
    ack(ui.loadBalancer, 'Ingress is port 80 from the VPC CIDR only (internal ALB, reached through the SSM tunnel); '
      + 'the rule cannot evaluate the CIDR token.', 'AwsSolutions-EC23');
    ack(accessLogsBucket, 'This is the access-log destination; logging it into itself would loop.', 'AwsSolutions-S1');
    const bucketDeployFn = this.node.findChild('Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C');
    ack(bucketDeployFn, 'CDK BucketDeployment singleton: copies the ACL file from the CDK asset bucket into the data bucket.',
      ...s3Read, ...s3Write, iam5(`Resource::<${lid(dataBucket)}.Arn>/*`), iam5(`Resource::arn:${P}:s3:::${assetBucket}/*`));
    ack(bucketDeployFn, 'CDK BucketDeployment singleton: its runtime is pinned by aws-cdk-lib.', 'AwsSolutions-L1');
    ack(riProvider, 'The CDK provider-framework waiter state machine has no X-Ray option; its execution history is '
      + 'logged to CloudWatch Logs at level ALL.', 'AwsSolutions-SF2');
    ack(riProvider, 'Step Functions log delivery: the CloudWatch Logs *LogDelivery and resource-policy APIs have no '
      + 'resource-level scoping.', anyResource);

    // ---------------------------------------------------------------- outputs
    new cdk.CfnOutput(this, 'KnowledgeBaseId', { value: kb.attrKnowledgeBaseId });
    new cdk.CfnOutput(this, 'DataSourceId', { value: dataSource.attrDataSourceId });
    new cdk.CfnOutput(this, 'DataBucketName', { value: dataBucket.bucketName });
    new cdk.CfnOutput(this, 'LogBucketName', { value: logBucket.bucketName });
    new cdk.CfnOutput(this, 'KbLogGroupName', { value: kbLogGroup.logGroupName });
    new cdk.CfnOutput(this, 'UiInternalAlbDns', { value: ui.loadBalancer.loadBalancerDnsName });
    new cdk.CfnOutput(this, 'EcsClusterName', { value: cluster.clusterName });
    new cdk.CfnOutput(this, 'EcsServiceName', { value: ui.service.serviceName });
    for (const { preset, id } of instructions) {
      new cdk.CfnOutput(this, `RetrievalInstructionId${preset.key[0].toUpperCase()}${preset.key.slice(1)}`, { value: id });
    }
    new cdk.CfnOutput(this, 'WeatherSyncFunctionName', { value: weatherSync.functionName });
    new cdk.CfnOutput(this, 'TunnelCommand', { value: `./scripts/tunnel.sh ${this.stackName}` });
  }
}
