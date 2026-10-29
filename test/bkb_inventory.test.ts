import * as cdk from 'aws-cdk-lib/core';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { BkbInventoryStack } from '../lib/bkb_inventory-stack';
// Same feature flags as `cdk synth` (e.g. bucket-policy-based S3 access logging).
import { context } from '../cdk.json';

const template = Template.fromStack(new BkbInventoryStack(new cdk.App({ context }), 'TestStack', {
  env: { account: '111111111111', region: 'us-east-1' },
  documentAcls: [{ prefix: 'docs/', allow: ['alice@example.com'], deny: ['bob@example.com'] }],
}));

test('KB is MANAGED with an ACL-enabled S3 connector over docs/', () => {
  template.resourceCountIs('AWS::S3Vectors::Index', 0);
  template.hasResourceProperties('AWS::Bedrock::KnowledgeBase', {
    KnowledgeBaseConfiguration: { Type: 'MANAGED', ManagedKnowledgeBaseConfiguration: { EmbeddingModelType: 'MANAGED' } },
  });
  const ds = Object.values(template.findResources('AWS::Bedrock::DataSource'))[0];
  const cfg = ds.Properties.DataSourceConfiguration;
  expect(cfg.Type).toBe('MANAGED_KNOWLEDGE_BASE_CONNECTOR');
  const params = cfg.ManagedKnowledgeBaseConnectorConfiguration.ConnectorParameters;
  expect(params).toMatchObject({ type: 'S3', version: '1', aclEnabled: true, filterConfiguration: { inclusionPrefixes: ['docs/'] } });
  expect(JSON.stringify(params.aclConfiguration)).toContain('/acl/global-acl.json');
});

test('global ACL file is deployed outside docs/ and ingestion waits for it', () => {
  const deployments = JSON.stringify(template.findResources('Custom::CDKBucketDeployment'));
  expect(deployments).toContain('"DestinationBucketKeyPrefix":"acl/"');
  const res = Object.values(template.findResources('Custom::AWS'))[0];
  expect((res.DependsOn as string[]).some(d => d.startsWith('DeployAcl'))).toBe(true);
  expect(JSON.stringify(res.Properties)).toContain('acl-hash=');
});

test('KB application logs go to both S3 and CloudWatch', () => {
  template.hasResourceProperties('AWS::Logs::DeliverySource', { LogType: 'APPLICATION_LOGS' });
  template.resourceCountIs('AWS::Logs::DeliveryDestination', 2);
  template.resourceCountIs('AWS::Logs::Delivery', 2);
});

test('ingestion starts only after docs and log deliveries exist', () => {
  const [id, res] = Object.entries(template.findResources('Custom::AWS'))[0];
  expect(id).toMatch(/^StartIngestion/);
  const deps: string[] = res.DependsOn;
  for (const prefix of ['WeatherDocsSync', 'KbS3Delivery', 'KbCwlDelivery']) {
    expect(deps.some(d => d.startsWith(prefix))).toBe(true);
  }
});

test('nothing is internet-facing', () => {
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', { Scheme: 'internal' });
  template.hasResourceProperties('AWS::ECS::Service', {
    EnableExecuteCommand: true,
    NetworkConfiguration: { AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: 'DISABLED' }) },
  });
  // No ingress from outside the VPC (egress to 0.0.0.0/0 via NAT is fine).
  const ingress = JSON.stringify([
    ...Object.values(template.findResources('AWS::EC2::SecurityGroup')).map(r => r.Properties.SecurityGroupIngress ?? []),
    ...Object.values(template.findResources('AWS::EC2::SecurityGroupIngress')),
  ]);
  expect(ingress).not.toContain('0.0.0.0/0');
  expect(ingress).not.toContain('::/0');
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', { HealthCheckPath: '/_stcore/health' });
});

test('UI image is built in CodeBuild (ARM) and the service waits for it', () => {
  template.hasResourceProperties('AWS::CodeBuild::Project', {
    Environment: Match.objectLike({ Type: 'ARM_CONTAINER', PrivilegedMode: true }),
  });
  const svc = Object.entries(template.findResources('AWS::ECS::Service'))[0][1];
  expect((svc.DependsOn as string[]).some(d => d.startsWith('UiImageBuilt'))).toBe(true);
  // No local Docker asset in the template.
  expect(JSON.stringify(template.toJSON())).not.toContain('cdk-hnb659fds-container-assets');
});

test('UI task can chat: Retrieve on the KB and Converse on the configured Claude profiles', () => {
  const policies = JSON.stringify(template.findResources('AWS::IAM::Policy'));
  expect(policies).toContain('bedrock:Retrieve');
  expect(policies).toContain('inference-profile/us.anthropic.claude-sonnet-5-5');
  expect(policies).toContain('foundation-model/anthropic.claude-sonnet-5-5*');
  expect(policies).not.toMatch(/"Action":"bedrock:\*"/);
  const env = JSON.stringify(template.findResources('AWS::ECS::TaskDefinition'));
  expect(env).toContain('CHAT_MODELS');
  expect(env).toContain('alice@example.com');  // ACL_DEFAULT_USER
});

test('UI task role is read-only on the KB: no sync, no ACL file access', () => {
  const taskRole = Object.keys(template.findResources('AWS::IAM::Role')).find(id => id.startsWith('UiTaskDefTaskRole'))!;
  const policy = Object.values(template.findResources('AWS::IAM::Policy'))
    .find(p => JSON.stringify(p.Properties.Roles).includes(taskRole))!;
  const doc = JSON.stringify(policy.Properties.PolicyDocument);
  for (const a of ['bedrock:StartIngestionJob', 'bedrock:StopIngestionJob', 'bedrock:UpdateDataSource', 'bedrock:Ingest']) {
    expect(doc).not.toContain(a);
  }
  expect(doc).toContain('/docs/*');
  expect(doc).not.toContain('/acl/');
});

test('retrieval instructions are deployed by a custom resource that waits for the review', () => {
  const ris = Object.values(template.findResources('Custom::BedrockRetrievalInstruction'));
  expect(ris.map(r => r.Properties.Name).sort()).toEqual(['TestStack-aviation', 'TestStack-weatherman']);
  for (const r of ris) {
    expect(Object.keys(r.Properties.Instruction).sort())
      .toEqual(['answerFormat', 'answerPolicy', 'persona', 'retrievalGuidance']);
  }
  // Provider framework with an isComplete handler = a waiter state machine.
  template.resourceCountIs('AWS::StepFunctions::StateMachine', 1);
});

test('UI task can run agentic retrieval, apply only its own instructions and check document ACLs', () => {
  const taskRole = Object.keys(template.findResources('AWS::IAM::Role')).find(id => id.startsWith('UiTaskDefTaskRole'))!;
  const policy = Object.values(template.findResources('AWS::IAM::Policy'))
    .find(p => JSON.stringify(p.Properties.Roles).includes(taskRole))!;
  const statements: any[] = policy.Properties.PolicyDocument.Statement;
  const actions = (s: any) => [s.Action].flat();
  expect(statements.some(s => actions(s).includes('bedrock:AgenticRetrieveStream'))).toBe(true);
  expect(statements.some(s => actions(s).includes('bedrock:GetDocumentContent'))).toBe(true);
  const get = statements.find(s => actions(s).includes('bedrock:GetRetrievalInstruction'));
  expect(JSON.stringify(get.Resource)).toContain('RetrievalInstructionweatherman');
  expect(JSON.stringify(get.Resource)).not.toContain('retrieval-instruction/*');
  for (const a of ['bedrock:CreateRetrievalInstruction', 'bedrock:UpdateRetrievalInstruction', 'bedrock:DeleteRetrievalInstruction']) {
    expect(JSON.stringify(policy)).not.toContain(a);
  }
  const env = JSON.stringify(template.findResources('AWS::ECS::TaskDefinition'));
  expect(env).toContain('RETRIEVAL_INSTRUCTIONS');
  expect(env).toContain('DOCUMENT_ACLS');
});

test('weather docs are mirrored S3-to-S3 by a Lambda, not uploaded from the repo', () => {
  // No BucketDeployment into docs/ (only the ACL file is deployed from CDK).
  const deployments = Object.values(template.findResources('Custom::CDKBucketDeployment'));
  expect(deployments.map(d => d.Properties.DestinationBucketKeyPrefix)).toEqual(['acl/']);
  const sync = Object.values(template.findResources('Custom::NoaaGsodSync'))[0];
  expect(sync.Properties).toMatchObject({ FromYear: '2020', DestPrefix: 'docs/weather/' });
  expect(JSON.parse(sync.Properties.Stations)).toHaveLength(46);
  // Ingestion description carries the sync fingerprint.
  const ingestion = JSON.stringify(Object.values(template.findResources('Custom::AWS'))[0].Properties);
  expect(ingestion).toContain('WeatherDocsSync');
  expect(ingestion).toContain('Fingerprint');
  // Source: list (year discovery) + read; the schedule may start ingestion.
  const policies = JSON.stringify(template.findResources('AWS::IAM::Policy'));
  expect(policies).toContain(':s3:::noaa-gsod-pds/*');
  expect(policies).toContain('s3:ListBucket');
  template.hasResourceProperties('AWS::Events::Rule', { ScheduleExpression: 'rate(1 day)' });
});
