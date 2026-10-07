# Single Recovery Account — Architecture

## 1. Purpose

This document describes how protected Prod workloads are restored into **one long-lived Recovery App account** in the Bunker Org, validated there, and backed up into **per-application logically air-gapped (LAG) vaults**. Those vaults can later be shared back to the Prod Org, where each application will run again.

It supersedes the earlier design, which vended a disposable Recovery App account per recovery and authorized each one through Prod SCP changes. That design's components are parked and are not used here.

## 2. Scope

**In scope**

- A persistent recovery foundation (network, private access, AWS Backup roles) in the recovery account.
- A per-application **app environment**: LAG vault, staging vault, KMS key, and security groups.
- Restoring **Amazon EFS, Amazon RDS for SQL Server, Amazon Aurora PostgreSQL, and Amazon S3** from the shared Prod LAG vault.
- Validating restored data from a private validation host through Session Manager.
- Backing up validated data into the application's LAG vault.
- Tearing down app environments and the foundation through runbooks.

**Out of scope for now (parked)**

- Changing the Prod SCP. A static SCP already allows the Prod LAG vault to be shared with the recovery account.
- Automating the share and unshare of the Prod LAG vault. A Prod operator does it by hand.
- Sharing the per-app LAG vaults back to the Prod Org and restoring there.
- Automated cleanup and revocation triggers.
- Other Regions. The demo and first implementation use **ap-east-1** only.

## 3. Key decisions

| # | Decision | Reason |
| --- | --- | --- |
| D1 | One long-lived Recovery App account | Removes account vending, decommissioning, and the 14-day quarantine that a locked vault imposed on disposable accounts |
| D2 | Static Prod SCP; control by sharing and unsharing the Prod LAG vault | No Prod Org change is needed for the demo; the share is the on/off switch |
| D3 | Runbooks live in Bunker Delegated Admin and run in the recovery account through SSM multi-account automation | One control plane; operators never sign in to the account holding restored data |
| D4 | SSM runbooks wrap plain CloudFormation stacks; no StackSet | Create and tear down on demand, with CloudFormation rollback and an exact teardown; one account does not need StackSets |
| D5 | Two layers: a persistent **foundation**, and one **app environment** per application | Applications can be restored in parallel without sharing vaults, keys, or network access |
| D6 | `/20` VPC with two `/24` subnets in two AZs, placed by AZ ID | Ample for EFS mount targets, databases, and validation hosts; AZ IDs are stable across accounts |
| D7 | No internet, NAT, VPN, or peering. Only an S3 gateway endpoint (package repositories and a tools bucket) and three interface endpoints | Restored data is untrusted, so the only paths out are to AWS service APIs |
| D8 | ssm, ssmmessages, and ec2messages interface endpoints in **one AZ** | Session Manager access to validation hosts at half the endpoint cost |
| D9 | One LAG vault, staging vault, and KMS key **per application**, with **14-day** minimum and maximum retention | Each application's recovery points can be shared with Prod independently; matches the original retention |
| D10 | The foundation's flow-log group and KMS key are **retained** on teardown | Evidence of each recovery survives teardown |
| D11 | The Prod LAG vault is encrypted with a **Bunker-owned CMK** | Granting the recovery account use of that key is a Bunker-side change |

## 4. Accounts and trust

```text
┌──────────────── Prod Org ────────────────┐
│  Prod LAG Vault account                  │
│   └─ LAG vault (encrypted with Bunker CMK)│── RAM share (manual; static SCP allows it) ──┐
└──────────────────────────────────────────┘                                              │
┌──────────────────────────── Bunker Org ─────────────────────────────────────────────────┼──┐
│  Bunker key account ── CMK ── key policy allows the recovery account via AWS Backup     │  │
│                                                                                          ▼  │
│  Delegated Admin (ap-east-1)                       Recovery App account (ap-east-1)          │
│   ├─ Recovery-ManageFoundation ──multi-account──▶  AWS-SystemsManager-AutomationExecutionRole │
│   ├─ Recovery-ManageAppEnvironment   (step 2)       └─ passes RecoveryCloudFormationRole       │
│   ├─ Restore runbook                 (step 3)           ├─ recovery-foundation stack           │
│   └─ AWS-SystemsManager-                                └─ recovery-app-<appId> stacks         │
│      AutomationAdministrationRole                                                              │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

| Role | Account | Can do |
| --- | --- | --- |
| `AWS-SystemsManager-AutomationAdministrationRole` | Delegated Admin | Assume only the recovery account's execution role |
| `AWS-SystemsManager-AutomationExecutionRole` | Recovery | Create, delete, and describe only the `recovery-foundation` and `recovery-app-*` stacks; pass only `RecoveryCloudFormationRole`; read-only checks of what was built |
| `RecoveryCloudFormationRole` | Recovery | The only identity that creates stack resources. Limited to ap-east-1, IAM roles named `recovery-*`, and approved managed policies |
| Backup service role (generated) | Recovery | AWS Backup restores and backups, including S3; use of the Bunker source CMK |
| Validation-host role (generated) | Recovery | Session Manager; read-only access to the tools bucket |

Both recovery-account roles are created once by `cloudformation/recovery-account-bootstrap.yaml`.

## 5. End-to-end flow

```text
0. One time        bootstrap roles · render · create documents · Recovery-ManageFoundation CREATE
1. Prod operator   share the Prod LAG vault with the recovery account (RAM)
2. Per app         Recovery-ManageAppEnvironment CREATE  AppId=<app>
3. Per app         restore runbook: accept the share, list recovery points, restore
                   EFS · RDS for SQL Server · Aurora PostgreSQL · S3 into the app environment
4. Per app         validation host (Session Manager): mount EFS, query the databases; check S3 via API
5. Per app         AWS Backup → the app's LAG vault
                   (EFS and S3 directly; SQL Server and Aurora via the app's staging vault, then copied)
6. Later           share the app's LAG vault and key with the new Prod account   (parked)
7. Per app         Recovery-ManageAppEnvironment DELETE: restored resources, then the stack
                   (the LAG vault is retained until its recovery points expire)
8. Prod operator   unshare the Prod LAG vault
```

Several applications can run steps 2–7 at the same time. Each has its own security groups, vaults, and key, and everything is tagged `AppId`.

## 6. Components

### 6.1 Foundation (`recovery-foundation`): implemented

```text
VPC /20 ── Subnet A /24 (AZ ID 1) ── route table A: local + S3 gateway
       └── Subnet B /24 (AZ ID 2) ── route table B: local + S3 gateway
S3 gateway endpoint ── GetObject on al2023-repos-<region>-* and the tools bucket only
Interface endpoints (subnet A) ── ssm · ssmmessages · ec2messages ── endpoint SG (HTTPS from app client SGs)
DB subnet group recovery-foundation-db (A + B)
VPC flow logs → KMS-encrypted log group (retained)
Backup service role · validation-host role and instance profile
SSM parameters /recovery/foundation/*
```

**`Recovery-ManageFoundation`**

- `CREATE` validates the inputs and creates the stack from the template embedded in the document, which is pinned by SHA-256. It then verifies the subnets, routes, endpoints, endpoint security group, parameters, and template hash. A rerun with identical inputs changes nothing. Different inputs, a different template, or a failed stack are refused: the foundation is never changed in place.
- `DELETE` refuses while any app environment exists or any non-endpoint network interface remains in the VPC. It then deletes the stack and retains the flow logs and key.

### 6.2 App environment (`recovery-app-<appId>`): step 2

| Resource | Purpose |
| --- | --- |
| LAG vault (14-day minimum and maximum, encrypted with the app key) | Destination for the app's validated backups, and the unit later shared with Prod |
| Standard staging vault | Temporary `DELETE_AFTER_COPY` points for RDS for SQL Server and Aurora before they are copied to the LAG vault |
| App KMS key | Encrypts restored resources and both vaults; the key policy lets the Backup service role use it |
| Security groups | Validation client; EFS (2049 from client); SQL Server (1433 from client); Aurora PostgreSQL (5432 from client); HTTPS ingress on the foundation endpoint SG from this app's client SG |

Teardown first deletes restored resources tagged with the `AppId`, then the stack. A LAG vault that still holds locked recovery points is retained, and its key with it, until they expire.

### 6.3 Restore runbook: step 3

- Accepts the RAM invitation from the Prod LAG account.
- Lists recovery points and records the selected ones.
- Starts AWS Backup restore jobs into the app environment, with the network placement, security groups, encryption key, and `AppId` tags.
- Waits for completion and reports the restored resource IDs.

### 6.4 Validation and backup: step 4

- A validation host is launched with the foundation instance profile and the app's client security group. Tools come from the Amazon Linux repositories and the tools bucket.
- AWS Backup jobs write validated resources into the app's LAG vault, and the runbook verifies the copies.

## 7. Network design

| Item | Value |
| --- | --- |
| Region | ap-east-1 (opt-in) |
| VPC | One private `/20`, for example `10.240.0.0/20` |
| Subnets | The first two `/24`s, one per AZ, chosen by AZ ID |
| Routes | Local, plus the S3 gateway endpoint's prefix list only |
| Interface endpoints | ssm, ssmmessages, ec2messages, in subnet A only |
| Security groups | No address-based rules. Every group has an explicit egress list; groups without real egress carry a placeholder rule that matches no traffic |

## 8. Encryption and backup

- **Source:** the Prod LAG vault is encrypted with a Bunker-owned CMK. Its key policy must allow the recovery account to use it through AWS Backup in ap-east-1, and the foundation's Backup service role holds the matching IAM permission. For infrastructure testing, the foundation can be created with `SourceKmsKeyArn=NONE`, which grants no source-key access and records `NONE`. The restore runbook refuses to start until the foundation has been recreated with the real key.
- **Restored resources:** encrypted with the app's KMS key.
- **App vaults:** LAG vaults are locked with 14-day minimum and maximum retention. AWS Backup writes EFS and S3 directly into a LAG vault. RDS for SQL Server and Aurora are backed up first to the standard staging vault as `DELETE_AFTER_COPY` points, then copied into the LAG vault.
- **Consequence:** any backup taken during a rehearsal locks that app's vault for 14 days. Use a fresh `AppId` for each rehearsal, or take the backup only for the real demo.

## 9. Security controls and the single-account trade-off

A disposable account per recovery gave isolation by default: an account that held untrusted restored data was closed afterwards. A long-lived account needs that isolation to be designed in instead:

- **Per-app boundaries.** Each application has its own security groups, key, and vaults, so one app's validation host cannot reach another app's databases.
- **Tag discipline.** Every restored and copied resource is tagged `AppId`. Teardown and backups act only on matching tags.
- **Least-privilege deployment.** Only `RecoveryCloudFormationRole` creates resources, and only through the two approved stacks. Operators work from Delegated Admin.
- **No external paths.** Restored workloads cannot reach the internet. The S3 endpoint policy allows reads from the package repositories and the tools bucket only.
- **Evidence.** Flow logs are encrypted and retained, and every runbook execution is recorded in Systems Manager.
- **Hygiene.** Tear down every app environment after use, and consider rebuilding the foundation periodically and closing the VPC's default security group.

## 10. Implementation status

| Step | Component | Status |
| --- | --- | --- |
| 1 | Foundation template, `Recovery-ManageFoundation`, bootstrap roles | Implemented, with offline tests |
| 2 | App environment template and `Recovery-ManageAppEnvironment` | Next |
| 3 | Restore runbook | Planned |
| 4 | Validation host and backup into the app LAG vault | Planned |

## 11. Risks and items to confirm

1. **LAG vault support** in ap-east-1 for RDS for SQL Server and Aurora PostgreSQL. This is the largest risk to the demo.
2. **ap-east-1 must be enabled** in Delegated Admin, the recovery account, the Bunker key account, and the Prod LAG account.
3. **The Bunker CMK key policy** must include the recovery-account statement (see `README.md`).
4. **Whether the document must be shared** with the recovery account for multi-account automation of a custom document.
5. **That CloudFormation returns the embedded template byte for byte** from `GetTemplate`, which the plan and verify steps compare by SHA-256.
6. **The Amazon Linux 2023 repository bucket names** in ap-east-1 match `al2023-repos-ap-east-1-*`.
7. **The 14-day LAG retention** delays teardown of any app vault that has received backups.
