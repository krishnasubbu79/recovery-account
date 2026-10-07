# Single Recovery Account — Foundation

This repository builds the restore infrastructure for the **single, long-lived Recovery App account** in **ap-east-1**. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design and its trade-offs. It replaces an earlier model that used a disposable account per recovery and Prod SCP changes, which is parked:

- the Prod SCP that allows sharing to this account is static;
- the Prod LAG vault is shared and unshared by hand by a Prod operator;
- no StackSet is used. SSM runbooks in Bunker Delegated Admin create and tear down plain CloudFormation stacks in the recovery account.

| Step | Component | Status |
| --- | --- | --- |
| 1 | `Recovery-ManageFoundation` and the `recovery-foundation` stack | **Implemented** |
| 2 | `Recovery-ManageAppEnvironment` and `recovery-app-<appId>` stacks (per-app LAG vault, staging vault, key, security groups) | Next |
| 3 | Restore runbook (accept share, restore EFS, SQL Server, Aurora PostgreSQL, S3) | Planned |
| 4 | Validation host and backup into the app's LAG vault | Planned |

## How it runs

```text
Bunker Delegated Admin (ap-east-1)                       Recovery App account (ap-east-1)
──────────────────────────────────                       ────────────────────────────────
operator ─▶ Recovery-ManageFoundation ──multi-account──▶ AWS-SystemsManager-AutomationExecutionRole
            (AWS-SystemsManager-                           ├─ plan: validate inputs, check stack state, check dependencies
             AutomationAdministrationRole)                 ├─ aws:createStack / aws:deleteStack
                                                           │    └─ as RecoveryCloudFormationRole (creates the resources)
                                                           └─ verify: what was built matches the design
```

The execution role can create and delete only the `recovery-foundation` and `recovery-app-*` stacks, and can pass only `RecoveryCloudFormationRole`. That service role is the one identity that creates VPCs, endpoints, keys, and roles, and it can create IAM roles only with the `recovery-` prefix and attach only approved managed policies. The foundation template is embedded in the document at render time and pinned by SHA-256, so the runbook can only deploy the approved template.

## What the foundation contains

```text
recovery-foundation (ap-east-1)
├── VPC /20 ── Subnet A /24 (AZ ID 1) ── route table A: local + S3 gateway
│          └── Subnet B /24 (AZ ID 2) ── route table B: local + S3 gateway
├── S3 gateway endpoint: GetObject only on the Amazon Linux 2023 repositories and the validation-tools bucket
├── Interface endpoints in subnet A: ssm, ssmmessages, ec2messages
│     endpoint security group: no ingress until an app stack adds HTTPS from its validation client group
├── DB subnet group recovery-foundation-db (A + B), for RDS for SQL Server and Aurora PostgreSQL
├── VPC flow logs → KMS-encrypted log group        (log group and key retained on DELETE)
├── AWS Backup service role: backup + restore (including S3), may use the Bunker source CMK
├── Validation-host role + instance profile (Session Manager; reads the tools bucket if set)
└── SSM parameters /recovery/foundation/…  (read by the app stacks and the restore runbook)
```

## Runbook behaviour

**`Action=CREATE`**
1. Requires ap-east-1 and the configured recovery account.
2. Requires a private, aligned `/20`, two different available AZ IDs, and a source key in ap-east-1.
3. If no stack exists, it creates one. A failed create is rolled back and deleted, so a corrected rerun starts clean.
4. If a healthy stack exists with identical inputs and the approved template, it changes nothing.
5. If the stack has different inputs, a different template, or a failed state, it refuses. The foundation is never changed in place; run `DELETE`, then `CREATE`.
6. It verifies:
   - both subnets are the expected `/24`s in the requested AZ IDs, with no public IPs;
   - routes are local or the S3 gateway only;
   - all four endpoints are available;
   - the endpoint security group allows only HTTPS from security groups, with no address-based rules;
   - all parameters exist;
   - the stack's template hash is the approved one.

**`Action=DELETE`**
1. Refuses while any `recovery-app-*` stack exists, or while any network interface other than the foundation's own endpoints is in the VPC (a restored database, EFS mount target, or validation host).
2. Deletes the stack and confirms it is gone.
3. Retains the flow-log group and the foundation KMS key as evidence. The alias is removed, so a later `CREATE` works.

> **Deploying step 1?** Follow [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md): a step-by-step procedure with a check after every step. The sections below are a summary.

## Prerequisites

1. **Enable ap-east-1** (an opt-in Region) in Delegated Admin, the recovery account, the Bunker key account, and the Prod LAG account.
2. **Confirm AWS Backup support** in ap-east-1 for logically air-gapped vaults with RDS for SQL Server, Aurora PostgreSQL, EFS, and S3.
3. **Bunker source CMK key policy** (needed before restores, not to create the foundation). Allow the recovery account to use the key through AWS Backup, for example:

   ```json
   {
     "Sid": "AllowRecoveryAccountRestoresThroughBackup",
     "Effect": "Allow",
     "Principal": { "AWS": "arn:aws:iam::<RECOVERY_ACCOUNT_ID>:root" },
     "Action": ["kms:Decrypt", "kms:DescribeKey", "kms:ReEncryptFrom", "kms:CreateGrant"],
     "Resource": "*",
     "Condition": {
       "StringEquals": { "kms:ViaService": "backup.ap-east-1.amazonaws.com", "kms:CallerAccount": "<RECOVERY_ACCOUNT_ID>" }
     }
   }
   ```

   The foundation's Backup service role holds the matching IAM permission.
4. **Validation tools (optional).** `sqlcmd` is not in the Amazon Linux repositories. To use it, put the tools in an S3 bucket and pass its name as `ToolsBucketName`.

## One-time setup

```shell
cp deployment.env.example deployment.env   # git-ignored; fill it in
source deployment.env && scripts/render.sh
```

**Recovery account** (once):

```shell
aws cloudformation deploy --region ap-east-1 \
  --stack-name recovery-account-bootstrap \
  --template-file "$OUT_DIR/recovery-account/recovery-account-bootstrap.yaml" \
  --parameter-overrides DelegatedAdminAccountId="$DELEGATED_ADMIN_ACCOUNT_ID" \
  --capabilities CAPABILITY_NAMED_IAM
```

**Bunker Delegated Admin** (once):

```shell
aws iam create-role --role-name AWS-SystemsManager-AutomationAdministrationRole \
  --assume-role-policy-document "file://$OUT_DIR/delegated-admin/delegated-admin-automation-administration-role.trust.json"
aws iam put-role-policy --role-name AWS-SystemsManager-AutomationAdministrationRole \
  --policy-name AssumeRecoveryAccountExecutionRole \
  --policy-document "file://$OUT_DIR/delegated-admin/delegated-admin-automation-administration-role.permissions.json"

aws ssm create-document --region ap-east-1 --name Recovery-ManageFoundation \
  --document-type Automation --document-format YAML \
  --content "file://$OUT_DIR/delegated-admin/manage-foundation.yaml"

# Multi-account automation runs the document in the target account, so share it there.
aws ssm modify-document-permission --region ap-east-1 --name Recovery-ManageFoundation \
  --permission-type Share --account-ids-to-add "$RECOVERY_ACCOUNT_ID"
```

Attach `delegated-admin-operator.permissions.json` to approved operators, then delete `$OUT_DIR`.

## Run

```shell
EXEC_ROLE="arn:aws:iam::$RECOVERY_ACCOUNT_ID:role/AWS-SystemsManager-AutomationExecutionRole"

# Create (or confirm) the foundation
aws ssm start-automation-execution --region ap-east-1 \
  --document-name Recovery-ManageFoundation \
  --target-locations "Accounts=$RECOVERY_ACCOUNT_ID,Regions=ap-east-1,ExecutionRoleName=AWS-SystemsManager-AutomationExecutionRole" \
  --parameters "AutomationAssumeRole=$EXEC_ROLE,Action=CREATE,VpcCidr=10.240.0.0/20,AvailabilityZoneId1=ape1-az1,AvailabilityZoneId2=ape1-az2,SourceKmsKeyArn=<bunker-cmk-arn>"
# SourceKmsKeyArn=NONE builds the foundation without source-key access, for infrastructure testing.

# Tear it down (after every app environment is gone)
aws ssm start-automation-execution --region ap-east-1 \
  --document-name Recovery-ManageFoundation \
  --target-locations "Accounts=$RECOVERY_ACCOUNT_ID,Regions=ap-east-1,ExecutionRoleName=AWS-SystemsManager-AutomationExecutionRole" \
  --parameters "AutomationAssumeRole=$EXEC_ROLE,Action=DELETE"
```

Find the real AZ IDs with `aws ec2 describe-availability-zones --region ap-east-1 --query 'AvailabilityZones[].ZoneId'`.

## Tests

```shell
python3 -m unittest discover -s tests -v      # requires PyYAML
```

26 offline tests cover:
- **CREATE planning:** absent, identical, different inputs, different template, failed and in-progress stacks, missing inputs, no source key yet, CIDR, AZ, key Region, and wrong Region or account;
- **DELETE planning:** absent, blocked by app stacks, blocked by resources in the VPC, and failed stacks;
- **Verification:** subnet size, routes out of the VPC, missing endpoints, endpoint security-group rules, and template hash;
- **Rendering:** the template is embedded byte-for-byte and its hash is pinned.

## To confirm on the first real run

- **Sharing the document with the target account** (the `modify-document-permission` step). Confirm that multi-account automation needs it for a custom document, or remove the step.
- **The stored template.** Check that CloudFormation's `GetTemplate` (`Original` stage) returns the template exactly as embedded. The plan and verify steps compare its SHA-256.
- **The Amazon Linux 2023 repository bucket names** in ap-east-1 match `al2023-repos-ap-east-1-*`. If they don't, `dnf` on the validation host is blocked by the S3 endpoint policy.
- **The VPC's default security group** still has AWS's default rules. Nothing uses it, but consider closing it (AWS Config rule `vpc-default-security-group-closed`).
