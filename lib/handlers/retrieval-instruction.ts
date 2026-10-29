// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Custom resource for a Bedrock retrieval instruction (no CloudFormation resource type exists yet).
 *
 * onEvent creates / updates / deletes; isComplete waits for the asynchronous content review and fails the
 * deployment with the review reasons if it is rejected. A failed update keeps serving the previous content,
 * and the CloudFormation rollback re-applies the old properties.
 * The SDK is bundled (not the Lambda runtime's), because retrieval instructions need a recent client.
 */
import {
  BedrockAgentClient, ConflictException, CreateRetrievalInstructionCommand, DeleteRetrievalInstructionCommand,
  GetRetrievalInstructionCommand, GetRetrievalInstructionResponse, ListRetrievalInstructionsCommand,
  ListRetrievalInstructionVersionsCommand, ResourceNotFoundException, UpdateRetrievalInstructionCommand,
} from '@aws-sdk/client-bedrock-agent';

const client = new BedrockAgentClient({});
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
const IN_REVIEW = ['CREATING', 'UPDATING'];

interface Props { Name: string; Description: string; Instruction: Record<string, string> }
interface Event {
  RequestType: 'Create' | 'Update' | 'Delete';
  PhysicalResourceId?: string;
  ResourceProperties: Props & { ServiceToken: string };
  OldResourceProperties?: Props;
}

const get = (id: string) => client.send(new GetRetrievalInstructionCommand({ retrievalInstructionId: id }));

/** Wait (bounded) for a review in progress: an update or delete during a review fails with ConflictException. */
async function settled(id: string, maxMs = 300_000): Promise<GetRetrievalInstructionResponse> {
  const t0 = Date.now();
  for (;;) {
    const ri = await get(id);
    if (!IN_REVIEW.includes(ri.lastTransitionStatus ?? '') || Date.now() - t0 > maxMs) return ri;
    await sleep(5000);
  }
}

async function findByName(name: string): Promise<string | undefined> {
  let nextToken: string | undefined;
  do {
    const page = await client.send(new ListRetrievalInstructionsCommand({ maxResults: 100, nextToken }));
    const hit = page.retrievalInstructionSummaries?.find(s => s.name === name);
    if (hit) return hit.retrievalInstructionId;
    nextToken = page.nextToken;
  } while (nextToken);
  return undefined;
}

async function update(id: string, p: Props): Promise<void> {
  const current = await settled(id);
  if (JSON.stringify(current.instruction ?? {}) === JSON.stringify(p.Instruction)
      && (current.description ?? '') === p.Description) {
    return;
  }
  // An update replaces the whole instruction: send every field, and the description (omitting it clears it).
  await client.send(new UpdateRetrievalInstructionCommand({
    retrievalInstructionId: id, description: p.Description, instruction: p.Instruction,
  }));
}

async function remove(id: string): Promise<void> {
  try {
    await settled(id);
    const versions: string[] = [];
    let nextToken: string | undefined;
    do {
      const page = await client.send(new ListRetrievalInstructionVersionsCommand({
        retrievalInstructionId: id, maxResults: 100, nextToken,
      }));
      versions.push(...(page.retrievalInstructionVersionSummaries ?? [])
        .map(v => v.version!).filter(v => v && v !== 'DRAFT'));
      nextToken = page.nextToken;
    } while (nextToken);
    // Numbered versions (e.g. from the test kit's --publish) block deleting the instruction.
    for (const v of versions) {
      await client.send(new DeleteRetrievalInstructionCommand({ retrievalInstructionId: id, retrievalInstructionVersion: v }));
    }
    await client.send(new DeleteRetrievalInstructionCommand({ retrievalInstructionId: id }));
  } catch (e) {
    if (!(e instanceof ResourceNotFoundException)) throw e;
  }
}

export async function onEvent(event: Event) {
  const p = event.ResourceProperties;
  console.log(event.RequestType, p.Name, event.PhysicalResourceId ?? '');
  if (event.RequestType === 'Delete') {
    // Also runs on rollback when the review rejected a new instruction. (The provider framework itself skips
    // the Delete of a create that failed before returning an id.)
    if (event.PhysicalResourceId) await remove(event.PhysicalResourceId);
    return { PhysicalResourceId: event.PhysicalResourceId };
  }
  if (event.RequestType === 'Update' && event.OldResourceProperties?.Name === p.Name) {
    await update(event.PhysicalResourceId!, p);
    return { PhysicalResourceId: event.PhysicalResourceId };
  }
  // Create, or a rename (new physical id; CloudFormation then deletes the old one).
  // Names are unique per account: adopt a leftover with the same name (e.g. after a failed rollback).
  const existing = await findByName(p.Name);
  if (existing) {
    await update(existing, p);
    return { PhysicalResourceId: existing };
  }
  try {
    const r = await client.send(new CreateRetrievalInstructionCommand({
      name: p.Name, description: p.Description, instruction: p.Instruction,
    }));
    return { PhysicalResourceId: r.retrievalInstructionId! };
  } catch (e) {
    if (e instanceof ConflictException) throw new Error(`Retrieval instruction ${p.Name} already exists: ${e.message}`);
    throw e;
  }
}

export async function isComplete(event: Event & { PhysicalResourceId: string }) {
  if (event.RequestType === 'Delete') return { IsComplete: true };
  const ri = await get(event.PhysicalResourceId);
  const status = ri.lastTransitionStatus ?? '';
  console.log(event.PhysicalResourceId, status, ri.status);
  if (IN_REVIEW.includes(status)) return { IsComplete: false };
  if (status.endsWith('_FAILED')) {
    const reasons = (ri.lastTransitionStatusReasons ?? [])
      .map(r => `${r.code}${r.fieldName ? ` (${r.fieldName})` : ''}: ${r.message ?? ''}`).join('; ');
    throw new Error(`Review of retrieval instruction ${ri.name} failed (${status}): ${reasons}`);
  }
  return {
    IsComplete: true,
    Data: { RetrievalInstructionId: ri.retrievalInstructionId, Arn: ri.retrievalInstructionArn, Status: ri.status },
  };
}
