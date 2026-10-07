# Step 1 Deployment Guide — Recovery Foundation

This guide deploys **step 1** only: the one-time roles, the `Recovery-ManageFoundation` runbook, and the `recovery-foundation` stack in the recovery account in **ap-east-1**. App environments (step 2) and restores (step 3) come later.

Each step says **which account** to use and ends with a **check**. Don't move on until the check passes.

| Part | Account | What happens | Time |
| --- | --- | --- | --- |
| A | Your machine | Fill in values, render artifacts, run tests | 10 min |
| B | Recovery account | Deploy the bootstrap roles | 5 min |
| C | Delegated Admin | Create the automation role and the runbook | 10 min |
| D | Delegated Admin → recovery | Run `CREATE` and watch it | 10–15 min |
| E | Recovery account | Check what was built | 10 min |
| F | Delegated Admin → recovery | Prove a rerun changes nothing | 5 min |
| G (optional) | Delegated Admin → recovery | Prove teardown works, then recreate | 20 min |

---

## Before you start

**Tools:** AWS CLI v2, `jq`, `shasum`, `bash`, and Python 3 with PyYAML (for the tests). `uvx` is optional, for `cfn-lint`.

**Access:**

| Account | You need |
| --- | --- |
| Recovery App account | Permission to deploy a CloudFormation stack that creates IAM roles (part B), and read-only access for checks (part E) |
| Bunker Delegated Admin | Permission to create an IAM role, create and share an SSM document, and start automations (parts C, D) |

**Values to collect:**

| Value | Where to find it |
| --- | --- |
| Delegated Admin and recovery account IDs | Organizations console, or `aws sts get-caller-identity` |
| Two AZ IDs in ap-east-1 | Part A, step 3 |
| VPC CIDR | A private `/20` that does not overlap networks you may connect later, for example `10.240.0.0/20` |
| Bunker CMK ARN (optional now) | The key that encrypts the Prod LAG vault, in the Bunker key account, ap-east-1. Use `NONE` to test the infrastructure; restores need the real key |
| Tools bucket (optional) | An S3 bucket with `sqlcmd` for the validation host, or `NONE` |

**ap-east-1 must be enabled.** It is an opt-in Region. Check each account with its own credentials:

```shell
aws account get-region-opt-status --region-name ap-east-1 --query RegionOptStatus --output text
# expected: ENABLED   (or ENABLED_BY_DEFAULT)
```

If it says `DISABLED`, enable it with `aws account enable-region --region-name ap-east-1` and wait until the status is `ENABLED`. This can take a few minutes.

> **Not needed yet:** the Bunker CMK and its key policy. With `SOURCE_KMS_KEY_ARN='NONE'` the foundation is built without source-key access. Before restores start in step 3, rerun `DELETE` and then `CREATE` with the real key ARN, and add the key-policy statement from the README. That takes about 10 minutes while no app environments exist.

---

## Part A — Prepare on your machine

**1. Clone the repository**, ideally outside OneDrive:

```shell
git clone https://github.com/krishnasubbu79/recovery-account.git
cd recovery-account
```

**2. Fill in your values:**

```shell
cp deployment.env.example deployment.env      # git-ignored
# edit deployment.env: account IDs, profile names, CIDR, key ARN, tools bucket
source deployment.env
```

**3. Find the AZ IDs** (any account works, because AZ IDs are the same everywhere):

```shell
aws ec2 describe-availability-zones --region ap-east-1 --profile "$RECOVERY_PROFILE" \
  --query 'AvailabilityZones[?State==`available`].[ZoneId,ZoneName]' --output table
```

Pick two different IDs, for example `ape1-az1` and `ape1-az2`. Put them in `AZ_ID_1` and `AZ_ID_2`, then run `source deployment.env` again. Subnet A and the interface endpoints go in `AZ_ID_1`.

**4. Check both profiles point at the right accounts:**

```shell
test "$(aws sts get-caller-identity --profile "$DA_PROFILE" --query Account --output text)" = "$DELEGATED_ADMIN_ACCOUNT_ID" && echo "DA profile OK"
test "$(aws sts get-caller-identity --profile "$RECOVERY_PROFILE" --query Account --output text)" = "$RECOVERY_ACCOUNT_ID" && echo "Recovery profile OK"
```

**5. Run the tests and render:**

```shell
python3 -m unittest discover -s tests          # expected: OK
uvx --from cfn-lint cfn-lint cloudformation/*.yaml --regions ap-east-1   # optional; expected: no output
scripts/render.sh
```

✅ **Check:** the render prints `render: foundation template SHA-256 <hash>, document <n> bytes`. Note the hash; it's the version of the foundation you are deploying.

---

## Part B — Recovery account: bootstrap roles

This creates `AWS-SystemsManager-AutomationExecutionRole`, which only Delegated Admin's automation role can assume, and `RecoveryCloudFormationRole`.

```shell
aws cloudformation deploy --region ap-east-1 --profile "$RECOVERY_PROFILE" \
  --stack-name recovery-account-bootstrap \
  --template-file "$OUT_DIR/recovery-account/recovery-account-bootstrap.yaml" \
  --parameter-overrides DelegatedAdminAccountId="$DELEGATED_ADMIN_ACCOUNT_ID" RecoveryRegion=ap-east-1 \
  --capabilities CAPABILITY_NAMED_IAM
```

✅ **Check:**

```shell
aws cloudformation describe-stacks --region ap-east-1 --profile "$RECOVERY_PROFILE" \
  --stack-name recovery-account-bootstrap --query 'Stacks[0].[StackStatus,Outputs]' --output json
```

Expect `CREATE_COMPLETE`, with outputs ending in `role/AWS-SystemsManager-AutomationExecutionRole` and `role/RecoveryCloudFormationRole`.

---

## Part C — Delegated Admin: automation role and runbook

**1. Create the automation administration role.** It can only assume the recovery account's execution role.

```shell
aws iam create-role --profile "$DA_PROFILE" \
  --role-name AWS-SystemsManager-AutomationAdministrationRole \
  --assume-role-policy-document "file://$OUT_DIR/delegated-admin/delegated-admin-automation-administration-role.trust.json"

aws iam put-role-policy --profile "$DA_PROFILE" \
  --role-name AWS-SystemsManager-AutomationAdministrationRole \
  --policy-name AssumeRecoveryAccountExecutionRole \
  --policy-document "file://$OUT_DIR/delegated-admin/delegated-admin-automation-administration-role.permissions.json"
```

If the role already exists (another team may use multi-account automation), **don't recreate it**. Add only the `AssumeRecoveryAccountExecutionRole` inline policy, and check that its trust policy allows `ssm.amazonaws.com`.

**2. Create the runbook and share it with the recovery account:**

```shell
aws ssm create-document --region ap-east-1 --profile "$DA_PROFILE" \
  --name Recovery-ManageFoundation --document-type Automation --document-format YAML \
  --content "file://$OUT_DIR/delegated-admin/manage-foundation.yaml"

aws ssm modify-document-permission --region ap-east-1 --profile "$DA_PROFILE" \
  --name Recovery-ManageFoundation --permission-type Share \
  --account-ids-to-add "$RECOVERY_ACCOUNT_ID"
```

**3. Give operators access.** Attach `$OUT_DIR/delegated-admin/delegated-admin-operator.permissions.json` to the operator role or group you'll use. Skip this if you're an administrator for the demo.

✅ **Check:**

```shell
aws ssm describe-document --region ap-east-1 --profile "$DA_PROFILE" --name Recovery-ManageFoundation \
  --query 'Document.[Status,DocumentType,DocumentVersion]' --output text
# expected: Active  Automation  1

aws ssm describe-document-permission --region ap-east-1 --profile "$DA_PROFILE" \
  --name Recovery-ManageFoundation --permission-type Share --query AccountIds --output text
# expected: the recovery account ID
```

---

## Part D — Run CREATE

**1. Start the automation from Delegated Admin.** It runs in the recovery account.

```shell
EXEC_ROLE="arn:aws:iam::$RECOVERY_ACCOUNT_ID:role/AWS-SystemsManager-AutomationExecutionRole"

PARENT_ID="$(aws ssm start-automation-execution --region ap-east-1 --profile "$DA_PROFILE" \
  --document-name Recovery-ManageFoundation \
  --target-locations "Accounts=$RECOVERY_ACCOUNT_ID,Regions=ap-east-1,ExecutionRoleName=AWS-SystemsManager-AutomationExecutionRole" \
  --parameters "AutomationAssumeRole=$EXEC_ROLE,Action=CREATE,VpcCidr=$VPC_CIDR,AvailabilityZoneId1=$AZ_ID_1,AvailabilityZoneId2=$AZ_ID_2,SourceKmsKeyArn=$SOURCE_KMS_KEY_ARN,ToolsBucketName=$TOOLS_BUCKET_NAME" \
  --query AutomationExecutionId --output text)"
echo "$PARENT_ID"
```

**2. Watch it.** The Delegated Admin execution is the parent. The work happens in a child execution in the recovery account.

```shell
# Parent status (Delegated Admin): InProgress → Success
aws ssm get-automation-execution --region ap-east-1 --profile "$DA_PROFILE" \
  --automation-execution-id "$PARENT_ID" --query 'AutomationExecution.AutomationExecutionStatus' --output text

# Child execution (recovery account). The shared document may appear by its ARN,
# so list recent executions and take the newest Recovery-ManageFoundation one.
aws ssm describe-automation-executions --region ap-east-1 --profile "$RECOVERY_PROFILE" --max-results 5 \
  --query 'AutomationExecutionMetadataList[].[AutomationExecutionId,DocumentName,AutomationExecutionStatus,ExecutionStartTime]' \
  --output table
CHILD_ID="<the newest Recovery-ManageFoundation execution ID from the table>"

# Its steps
aws ssm describe-automation-step-executions --region ap-east-1 --profile "$RECOVERY_PROFILE" \
  --automation-execution-id "$CHILD_ID" \
  --query 'StepExecutions[].[StepName,StepStatus,FailureMessage]' --output table
```

The console is easier for watching. In the recovery account, open **Systems Manager → Automation → Executions** in ap-east-1. Creating the stack is the slowest step; the interface endpoints take a few minutes.

**3. Read the result:**

```shell
aws ssm get-automation-execution --region ap-east-1 --profile "$RECOVERY_PROFILE" \
  --automation-execution-id "$CHILD_ID" --query 'AutomationExecution.Outputs' --output json
```

✅ **Check:** the steps run `PlanFoundationAction` (`CREATE_STACK`) → `ChooseFoundationAction` → `CreateFoundationStack` → `VerifyFoundation`, all `Success`. The outputs include `VerifyFoundation.Status = FOUNDATION_READY` and the VPC and subnet IDs.

---

## Part E — Check what was built (recovery account)

The runbook already verified these. Checking them yourself is how you'll know what to show and explain.

```shell
R="--region ap-east-1 --profile $RECOVERY_PROFILE"

# Stack status and outputs
aws cloudformation describe-stacks $R --stack-name recovery-foundation \
  --query 'Stacks[0].[StackStatus,Outputs[].[OutputKey,OutputValue]]' --output json

# Parameters the next steps will use
aws ssm get-parameters-by-path $R --path /recovery/foundation --query 'Parameters[].[Name,Value]' --output table

VPC_ID="$(aws ssm get-parameter $R --name /recovery/foundation/vpc-id --query Parameter.Value --output text)"

# Two /24 subnets, in the AZ IDs you chose, no public IPs
aws ec2 describe-subnets $R --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'Subnets[].[CidrBlock,AvailabilityZoneId,MapPublicIpOnLaunch]' --output table

# Routes: only "local" and the S3 prefix list via vpce-…; no igw-, nat-, tgw-, or pcx-
aws ec2 describe-route-tables $R --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'RouteTables[].Routes[].[DestinationCidrBlock||DestinationPrefixListId,GatewayId]' --output table

# Endpoints: ssm, ssmmessages, ec2messages (Interface) and s3 (Gateway), all available
aws ec2 describe-vpc-endpoints $R --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'VpcEndpoints[].[ServiceName,VpcEndpointType,State]' --output table
```

✅ **Check:**
- the stack is `CREATE_COMPLETE`;
- there are 10 parameters;
- both subnets are `/24`s with `False` public IPs;
- there are only local and S3 routes;
- all four endpoints are `available`.

---

## Part F — Prove a rerun changes nothing

Run the part D command again with the same values.

✅ **Check:** `PlanFoundationAction` reports `ALREADY_EXISTS`, `CreateFoundationStack` is skipped, and `VerifyFoundation` reports `FOUNDATION_READY`. Then run it once with a different `VpcCidr`. It should **fail** in `PlanFoundationAction` with "already exists with different VpcCidr … run DELETE, then CREATE", and nothing should change.

---

## Part G (optional) — Prove teardown works

Do this now, while nothing depends on the foundation. Then recreate it.

```shell
aws ssm start-automation-execution --region ap-east-1 --profile "$DA_PROFILE" \
  --document-name Recovery-ManageFoundation \
  --target-locations "Accounts=$RECOVERY_ACCOUNT_ID,Regions=ap-east-1,ExecutionRoleName=AWS-SystemsManager-AutomationExecutionRole" \
  --parameters "AutomationAssumeRole=$EXEC_ROLE,Action=DELETE"
```

✅ **Check:** `PlanFoundationAction` reports `DELETE_STACK`, then `DeleteFoundationStack` and `VerifyFoundationDeleted` succeed with `FOUNDATION_DELETED`. `describe-stacks --stack-name recovery-foundation` now says the stack does not exist.

**What stays behind, on purpose:** one flow-log group (named `recovery-foundation-FlowLogGroup-…`) and one KMS key, which no longer has its alias. Each create and delete cycle leaves one of each. To remove them after rehearsals:

```shell
aws logs describe-log-groups $R --log-group-name-prefix recovery-foundation-FlowLogGroup --query 'logGroups[].logGroupName'
aws logs delete-log-group $R --log-group-name <name>
aws kms schedule-key-deletion $R --key-id <key-id> --pending-window-in-days 7
```

Then run part D again to recreate the foundation.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `start-automation-execution` fails with `AccessDenied` on `AWS-SystemsManager-AutomationAdministrationRole` | The role is missing, or its trust policy doesn't allow `ssm.amazonaws.com` | Recheck part C, step 1 |
| Parent execution fails quickly; child never appears | The admin role can't assume the recovery account's execution role, or the document isn't shared | Check the bootstrap stack (part B), the inline policy (part C.1), and `describe-document-permission` (part C.2) |
| Start fails with a message about targets or rate control | This CLI version requires a target for multi-account runs | Send me the exact message. The workaround is to add a target parameter |
| `PlanFoundationAction`: "runs only in ap-east-1" or "must run in the recovery account" | Wrong Region or account in `--target-locations`, or the document was rendered with other values | Fix the values, render again, and update the document (`aws ssm update-document … --document-version '$LATEST'`, then `update-document-default-version`) |
| `PlanFoundationAction`: AZ IDs "not available" | Wrong AZ IDs, or ap-east-1 not enabled in the recovery account | Repeat part A, step 3, and the Region check |
| `CreateFoundationStack` fails | A resource couldn't be created. The stack is rolled back and deleted automatically | Find the first `CREATE_FAILED` event: `aws cloudformation describe-stack-events $R --stack-name recovery-foundation` works only while it exists, so also check **CloudFormation → Stacks → Deleted** in the console. Send me the reason |
| A `CREATE_FAILED` event says the `RecoveryCloudFormationRole` isn't authorized for an action | The service role lacks a permission AWS requires | Send me the action name; I'll add it to the bootstrap template |
| `VerifyFoundation`: "not created from the approved template" | CloudFormation returned the template body differently from how it was embedded | Send me the message. This is one of the items to confirm on the first run |
| `VerifyFoundation`: endpoints "not available" | Endpoints were still provisioning | Wait a minute and run `CREATE` again. It reports `ALREADY_EXISTS` and verifies again |
| `DELETE` refused: "still use the VPC" | Something is running in the VPC | Remove the listed network interfaces' owners, then retry |

---

## Undo everything from step 1

In this order:

1. Run `DELETE` (part G) and clean up the retained log group and key if you want.
2. **Delegated Admin:** `aws ssm delete-document --name Recovery-ManageFoundation`, then delete `AWS-SystemsManager-AutomationAdministrationRole`'s inline policy and the role itself, if nothing else uses it.
3. **Recovery account:** `aws cloudformation delete-stack --stack-name recovery-account-bootstrap`.

---

## What to send me after the run

So I can confirm the first-run items and fix anything before step 2:

1. Did `CREATE` reach `FOUNDATION_READY`? If not, which step failed, and its `FailureMessage`.
2. Did multi-account automation need the document to be shared (part C.2)? If you can, try once without sharing.
3. Any `CREATE_FAILED` event, especially an authorization error on `RecoveryCloudFormationRole`.
4. Whether part F (rerun, and refused change) behaved as described.
5. The output of the routes and endpoints checks in part E.
