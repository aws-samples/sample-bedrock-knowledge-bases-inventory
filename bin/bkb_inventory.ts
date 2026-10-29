#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { BkbInventoryStack } from '../lib/bkb_inventory-stack';

const app = new cdk.App();

new BkbInventoryStack(app, 'BkbInventoryStack', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION },
});

// Fails synth on any unsuppressed AwsSolutions finding; accepted ones are justified in the stack.
Validations.of(app).addPlugins(new AwsSolutionsChecks(app));
