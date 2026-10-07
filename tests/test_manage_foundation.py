"""Offline tests for Recovery-ManageFoundation.

The scripts embedded in the SSM document run against in-memory fakes of
CloudFormation, EC2, and SSM. The render test checks that the foundation
template is embedded byte-for-byte and pinned by its SHA-256.

    python3 -m unittest discover -s tests -v      (requires PyYAML)
"""
import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "ssm" / "manage-foundation.yaml"
TEMPLATE = ROOT / "cloudformation" / "foundation.yaml"
TEMPLATE_BODY = TEMPLATE.read_text()
SHA = hashlib.sha256(TEMPLATE_BODY.encode()).hexdigest()
ACCOUNT = "777777777777"
REGION = "ap-east-1"
KEY = "arn:aws:kms:ap-east-1:888888888888:key/1234abcd-12ab-34cd-56ef-1234567890ab"


class ClientError(Exception):
    def __init__(self, code, message=""):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": message}}


botocore = types.ModuleType("botocore")
botocore.exceptions = types.ModuleType("botocore.exceptions")
botocore.exceptions.ClientError = ClientError
sys.modules["botocore"] = botocore
sys.modules["botocore.exceptions"] = botocore.exceptions


def parameters(**overrides):
    values = {"VpcCidr": "10.240.0.0/20", "AvailabilityZoneId1": "ape1-az1", "AvailabilityZoneId2": "ape1-az2",
              "SourceKmsKeyArn": KEY, "ToolsBucketName": "NONE", "FlowLogRetentionDays": "90"}
    values.update(overrides)
    return [{"ParameterKey": k, "ParameterValue": v} for k, v in values.items()] + \
           [{"ParameterKey": "TemplateVersion", "ParameterValue": "1.0.0"}]


class FakeCloudFormation:
    def __init__(self):
        self.stack = None
        self.body = TEMPLATE_BODY
        self.other_stacks = []

    def describe_stacks(self, StackName):
        if self.stack is None:
            raise ClientError("ValidationError", "Stack with id recovery-foundation does not exist")
        return {"Stacks": [self.stack]}

    def get_template(self, StackName, TemplateStage):
        return {"TemplateBody": self.body}

    def list_stacks(self, StackStatusFilter, NextToken=None):
        return {"StackSummaries": [{"StackName": n} for n in self.other_stacks + ["recovery-foundation"]]}

    def describe_stack_resource(self, StackName, LogicalResourceId):
        return {"StackResourceDetail": {"PhysicalResourceId": "vpc-1"}}


class FakeEc2:
    def __init__(self):
        self.interfaces = [{"NetworkInterfaceId": "eni-endpoint", "InterfaceType": "vpc_endpoint"}]
        self.unavailable_zones = set()
        self.subnets = {
            "subnet-a": {"SubnetId": "subnet-a", "VpcId": "vpc-1", "CidrBlock": "10.240.0.0/24",
                         "AvailabilityZoneId": "ape1-az1", "MapPublicIpOnLaunch": False},
            "subnet-b": {"SubnetId": "subnet-b", "VpcId": "vpc-1", "CidrBlock": "10.240.1.0/24",
                         "AvailabilityZoneId": "ape1-az2", "MapPublicIpOnLaunch": False},
        }
        self.routes = [{"GatewayId": "local", "DestinationCidrBlock": "10.240.0.0/20"},
                       {"GatewayId": "vpce-s3", "DestinationPrefixListId": "pl-s3"}]
        self.endpoints = [{"ServiceName": "com.amazonaws.ap-east-1." + s, "VpcEndpointType": "Interface",
                           "State": "available"} for s in ("ssm", "ssmmessages", "ec2messages")] + \
                         [{"ServiceName": "com.amazonaws.ap-east-1.s3", "VpcEndpointType": "Gateway", "State": "available"}]
        self.endpoint_group = {
            "IpPermissions": [],
            "IpPermissionsEgress": [{"IpProtocol": "icmp", "IpRanges": [{"CidrIp": "255.255.255.255/32"}]}],
        }

    def describe_availability_zones(self, Filters):
        return {"AvailabilityZones": [{"ZoneId": z, "State": "available", "ZoneType": "availability-zone"}
                                      for z in Filters[0]["Values"] if z not in self.unavailable_zones]}

    def describe_network_interfaces(self, Filters, NextToken=None):
        return {"NetworkInterfaces": self.interfaces}

    def describe_subnets(self, SubnetIds):
        return {"Subnets": [self.subnets[i] for i in SubnetIds if i in self.subnets]}

    def describe_route_tables(self, Filters):
        return {"RouteTables": [{"RouteTableId": "rtb-a", "Routes": self.routes}]}

    def describe_vpc_endpoints(self, Filters):
        return {"VpcEndpoints": self.endpoints}

    def describe_security_groups(self, GroupIds):
        return {"SecurityGroups": [dict(self.endpoint_group, GroupId=GroupIds[0])]}


class FakeSsm:
    def __init__(self):
        self.values = {
            "vpc-id": "vpc-1", "vpc-cidr": "10.240.0.0/20", "subnet-a-id": "subnet-a", "subnet-b-id": "subnet-b",
            "db-subnet-group-name": "recovery-foundation-db", "endpoint-security-group-id": "sg-endpoints",
            "backup-service-role-arn": "arn:role", "validation-instance-profile-arn": "arn:profile",
            "source-kms-key-arn": KEY, "template-version": "1.0.0",
        }

    def get_parameters_by_path(self, Path, Recursive, NextToken=None):
        return {"Parameters": [{"Name": Path + "/" + k, "Value": v} for k, v in self.values.items()]}


class RunbookTest(unittest.TestCase):
    def setUp(self):
        self.cloudformation, self.ec2, self.ssm = FakeCloudFormation(), FakeEc2(), FakeSsm()
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = lambda service, **_: {
            "cloudformation": self.cloudformation, "ec2": self.ec2, "ssm": self.ssm}[service]
        sys.modules["boto3"] = fake_boto3
        doc = yaml.safe_load(DOCUMENT.read_text())
        self.steps = {s["name"]: s for s in doc["mainSteps"]}
        self.plan = self.handler("PlanFoundationAction", "plan")
        self.verify = self.handler("VerifyFoundation", "verify")

    def handler(self, step, name):
        namespace = {}
        exec(compile(self.steps[step]["inputs"]["Script"], step, "exec"), namespace)
        return namespace[name]

    def events(self, action="CREATE", **changes):
        events = {
            "action": action, "automationRegion": REGION, "accountId": ACCOUNT, "expectedAccountId": ACCOUNT,
            "approvedRegionsCsv": REGION, "approvedTemplateSha256": SHA, "vpcCidr": "10.240.0.0/20",
            "availabilityZoneIds": ["ape1-az1", "ape1-az2"], "sourceKmsKeyArn": KEY,
            "toolsBucketName": "NONE", "flowLogRetentionDays": "90",
        }
        events.update(changes)
        return events

    def existing(self, status="CREATE_COMPLETE", **parameter_overrides):
        self.cloudformation.stack = {"StackStatus": status, "Parameters": parameters(**parameter_overrides)}

    # --- CREATE ---
    def test_create_when_absent(self):
        self.assertEqual(self.plan(self.events(), None)["NextAction"], "CREATE_STACK")

    def test_identical_rerun_changes_nothing(self):
        self.existing()
        self.assertEqual(self.plan(self.events(), None)["NextAction"], "ALREADY_EXISTS")

    def test_different_inputs_are_refused(self):
        self.existing(VpcCidr="10.250.0.0/20")
        with self.assertRaisesRegex(ValueError, "different VpcCidr.*DELETE, then CREATE"):
            self.plan(self.events(), None)

    def test_different_template_is_refused(self):
        self.existing()
        self.cloudformation.body = TEMPLATE_BODY + "# changed\n"
        with self.assertRaisesRegex(ValueError, "different template version"):
            self.plan(self.events(), None)

    def test_failed_stack_must_be_deleted_first(self):
        self.existing(status="ROLLBACK_COMPLETE")
        with self.assertRaisesRegex(RuntimeError, "ROLLBACK_COMPLETE; run DELETE, then CREATE"):
            self.plan(self.events(), None)

    def test_stack_in_progress_is_refused(self):
        self.existing(status="CREATE_IN_PROGRESS")
        with self.assertRaisesRegex(RuntimeError, "wait for it to finish"):
            self.plan(self.events(), None)

    def test_create_requires_network_and_key_inputs(self):
        with self.assertRaisesRegex(ValueError, "CREATE requires VpcCidr, SourceKmsKeyArn"):
            self.plan(self.events(vpcCidr="NONE", sourceKmsKeyArn="NONE"), None)

    def test_public_or_misaligned_cidr_is_refused(self):
        with self.assertRaisesRegex(ValueError, "inside 10.0.0.0/8"):
            self.plan(self.events(vpcCidr="52.0.0.0/20"), None)
        with self.assertRaisesRegex(ValueError, "aligned network address"):
            self.plan(self.events(vpcCidr="10.240.1.0/20"), None)

    def test_same_or_unavailable_zones_are_refused(self):
        with self.assertRaisesRegex(ValueError, "must be different"):
            self.plan(self.events(availabilityZoneIds=["ape1-az1", "ape1-az1"]), None)
        self.ec2.unavailable_zones = {"ape1-az2"}
        with self.assertRaisesRegex(ValueError, "not available"):
            self.plan(self.events(), None)

    def test_source_key_in_another_region_is_refused(self):
        with self.assertRaisesRegex(ValueError, "must be in ap-east-1"):
            self.plan(self.events(sourceKmsKeyArn=KEY.replace("ap-east-1", "eu-west-2")), None)

    def test_wrong_region_or_account_is_refused(self):
        with self.assertRaisesRegex(ValueError, "runs only in ap-east-1"):
            self.plan(self.events(automationRegion="eu-west-2"), None)
        with self.assertRaisesRegex(ValueError, "must run in the recovery account"):
            self.plan(self.events(accountId="111111111111"), None)

    # --- DELETE ---
    def test_delete_when_absent_is_already_deleted(self):
        self.assertEqual(self.plan(self.events("DELETE"), None)["NextAction"], "ALREADY_DELETED")

    def test_delete_needs_no_network_inputs(self):
        self.existing()
        self.assertEqual(self.plan(self.events("DELETE", vpcCidr="NONE", sourceKmsKeyArn="NONE"), None)["NextAction"],
                         "DELETE_STACK")

    def test_delete_is_blocked_by_app_environments(self):
        self.existing()
        self.cloudformation.other_stacks = ["recovery-app-payroll"]
        with self.assertRaisesRegex(RuntimeError, "Tear down these app environments first: recovery-app-payroll"):
            self.plan(self.events("DELETE"), None)

    def test_delete_is_blocked_by_resources_still_in_the_vpc(self):
        self.existing()
        self.ec2.interfaces.append({"NetworkInterfaceId": "eni-rds", "Description": "RDSNetworkInterface"})
        with self.assertRaisesRegex(RuntimeError, "eni-rds \\(RDSNetworkInterface\\)"):
            self.plan(self.events("DELETE"), None)

    def test_failed_stack_can_be_deleted(self):
        self.existing(status="ROLLBACK_COMPLETE")
        self.assertEqual(self.plan(self.events("DELETE"), None)["NextAction"], "DELETE_STACK")

    # --- VERIFY ---
    def verify_events(self):
        return {"automationRegion": REGION, "approvedTemplateSha256": SHA}

    def test_verify_accepts_the_intended_foundation(self):
        self.existing()
        self.assertEqual(self.verify(self.verify_events(), None)["Status"], "FOUNDATION_READY")

    def test_verify_rejects_wrong_subnet_size(self):
        self.existing()
        self.ec2.subnets["subnet-a"]["CidrBlock"] = "10.240.0.0/28"
        with self.assertRaisesRegex(RuntimeError, "expected 10.240.0.0/24"):
            self.verify(self.verify_events(), None)

    def test_verify_rejects_a_route_out_of_the_vpc(self):
        self.existing()
        self.ec2.routes.append({"GatewayId": "igw-1", "DestinationCidrBlock": "0.0.0.0/0"})
        with self.assertRaisesRegex(RuntimeError, "route to 0.0.0.0/0 via igw-1"):
            self.verify(self.verify_events(), None)

    def test_verify_rejects_a_missing_endpoint(self):
        self.existing()
        self.ec2.endpoints = self.ec2.endpoints[1:]
        with self.assertRaisesRegex(RuntimeError, "com.amazonaws.ap-east-1.ssm"):
            self.verify(self.verify_events(), None)

    def test_verify_rejects_open_endpoint_group(self):
        self.existing()
        self.ec2.endpoint_group["IpPermissions"] = [
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
        with self.assertRaisesRegex(RuntimeError, "address-based ingress"):
            self.verify(self.verify_events(), None)

    def test_verify_accepts_https_from_app_client_groups(self):
        self.existing()
        self.ec2.endpoint_group["IpPermissions"] = [
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "UserIdGroupPairs": [{"GroupId": "sg-app"}]}]
        self.assertEqual(self.verify(self.verify_events(), None)["Status"], "FOUNDATION_READY")

    def test_verify_rejects_unapproved_template(self):
        self.existing()
        self.cloudformation.body = "Resources: {}\n"
        with self.assertRaisesRegex(RuntimeError, "not created from the approved template"):
            self.verify(self.verify_events(), None)


class RenderTest(unittest.TestCase):
    def test_render_embeds_the_template_verbatim_and_pins_its_hash(self):
        with tempfile.TemporaryDirectory() as out:
            env = dict(os.environ, RECOVERY_REGION=REGION, DELEGATED_ADMIN_ACCOUNT_ID="222222222222",
                       RECOVERY_ACCOUNT_ID=ACCOUNT, OUT_DIR=out + "/render")
            subprocess.run(["bash", str(ROOT / "scripts" / "render.sh")], env=env, check=True, capture_output=True)
            rendered = yaml.safe_load(pathlib.Path(out, "render", "delegated-admin", "manage-foundation.yaml").read_text())
        steps = {s["name"]: s for s in rendered["mainSteps"]}
        self.assertEqual(steps["CreateFoundationStack"]["inputs"]["TemplateBody"], TEMPLATE_BODY)
        self.assertEqual(steps["PlanFoundationAction"]["inputs"]["InputPayload"]["approvedTemplateSha256"], SHA)
        self.assertEqual(steps["VerifyFoundation"]["inputs"]["InputPayload"]["approvedTemplateSha256"], SHA)
        self.assertEqual(steps["CreateFoundationStack"]["inputs"]["RoleARN"],
                         "arn:aws:iam::%s:role/RecoveryCloudFormationRole" % ACCOUNT)


if __name__ == "__main__":
    unittest.main()
