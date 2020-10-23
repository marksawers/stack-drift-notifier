#!/usr/bin/env python3

import boto3
import json
import os
import re
from botocore.exceptions import ClientError
from datetime import datetime, timedelta, timezone
from time import sleep

from decorators import retry
from sns_logger import SNSlogger

# Constants
ACTIVE_STACK_STATUS=[
    'CREATE_IN_PROGRESS',
    'CREATE_FAILED',
    'CREATE_COMPLETE',
    'ROLLBACK_IN_PROGRESS',
    'ROLLBACK_FAILED',
    'ROLLBACK_COMPLETE',
    'UPDATE_IN_PROGRESS',
    'UPDATE_COMPLETE_CLEANUP_IN_PROGRESS',
    'UPDATE_COMPLETE',
    'UPDATE_ROLLBACK_IN_PROGRESS',
    'UPDATE_ROLLBACK_FAILED',
    'UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS',
    'UPDATE_ROLLBACK_COMPLETE',
    'REVIEW_IN_PROGRESS'
]

DEFAULT_REGIONS = [
    'ap-south-1',
    'eu-west-3',
    'eu-west-2',
    'eu-west-1',
    'ap-northeast-2',
    'ap-northeast-1',
    'sa-east-1',
    'ca-central-1',
    'ap-southeast-1',
    'ap-southeast-2',
    'eu-central-1',
    'us-east-1',
    'us-east-2',
    'us-west-1',
    'us-west-2'
]
DEFAULT_LAST_CHECK_THRESHOLD_SECS = '60'
DEFAULT_REPORT_AGGREGATE = 'true'
REPORT_RESOURCES_NONE = 'None'
REPORT_RESOURCES_NAMEONLY = "NameOnly"
REPORT_RESOURCES_DETAILED = "Detailed"
REPORT_RESOURCES_OPTIONS = [ REPORT_RESOURCES_NONE, REPORT_RESOURCES_NAMEONLY, REPORT_RESOURCES_DETAILED ]
DEFAULT_REPORT_RESOURCES = REPORT_RESOURCES_NAMEONLY
DEFAULT_SNS_SUBJECT = 'CFN Drift Detector Report'

class DriftDetector(object):
    '''
    DriftDetector is a class that runs drift detection on every stack, and,
    depending on the configuration, writes one or more reports to the logger.
    '''
    stacks = []
    failed_stack_ids = []

    def __init__(self,profile,region,logger,last_check_threshold,report_aggregate,report_resources):
        self.region = region
        self.logger = logger
        self.report_aggregate = report_aggregate
        self.report_resources = report_resources

        session = boto3.session.Session(
                      profile_name=profile,
                      region_name=region
                  )
        self.account = session.client('sts').get_caller_identity()['Account']
        self.start_time = datetime.now(timezone.utc)
        self.cfn_client = session.client('cloudformation')
        self.logger.log.info("Checking drift on account: {}, region: {}".format(self.region, self.account))
        self.detections = self.check_drift(last_check_threshold)
        self.logger.log.info("Detections: {}".format(self.detections))
        self.wait_for_detection()
        self.report()


    def _get_stacks(self):
        '''
        Retreives all active stacks
        '''
        resp = self._cfn_call('list_stacks',{'StackStatusFilter':ACTIVE_STACK_STATUS})
        stacks = resp['StackSummaries']
        while stacks == None or 'NextToken' in resp.keys():
            print(resp)
            resp = self._cfn_call('list_stacks',{'NextToken':resp['NextToken']})
            stacks.append(resp['StackSummaries'])
        return stacks


    def check_drift(self,last_check_threshold):
        '''
        Checks every stack for drift
        '''
        detections = []
        for stack in self._get_stacks():
            resp = None
            check_threshold = datetime.now(timezone.utc) - timedelta(seconds=last_check_threshold)
            print('Stack: {}'.format(stack))
            if stack.get('DriftInformation', {'StackDriftStatus':'NOT_CHECKED'})['StackDriftStatus'] == 'NOT_CHECKED':
                resp = self._detect(stack['StackName'])
            else:
                if 'LastCheckTimestamp' in stack['DriftInformation'] and stack['DriftInformation']['LastCheckTimestamp'] < check_threshold:
                    resp = self._detect(stack['StackName'])
            if resp:
                detections.append(resp)
        return detections


    def _detect(self,stack_name):
        '''
        Private method for making the detect request with exception handling
        '''
        # I really wish there was a list drift operations and status so this was not necessay
        try:
            resp = self._cfn_call('detect_stack_drift',{'StackName':stack_name})
            resp['StackName'] = stack_name
            return resp
        except ClientError as e:
            if 'Drift detection is already in progress for stack' in e.response['Error']['Message']:
                self.log.critical(e.response['Error']['Message'])
            return None

    def _describe_resource_drifts(self,stack_name):
        resource_drifts = {}
        resp = self._cfn_call('describe_stack_resource_drifts',{
            'StackName':stack_name,
            'StackResourceDriftStatusFilters': ['MODIFIED','DELETED']
        })
        for resource in resp['StackResourceDrifts']:
            if resource['StackResourceDriftStatus'] not in resource_drifts:
                resource_drifts[resource['StackResourceDriftStatus']] = []
            if self.report_resources == REPORT_RESOURCES_DETAILED:
                resource_drifts[resource['StackResourceDriftStatus']].append({
                    'LogicalResourceId': resource['LogicalResourceId'],
                    'ResourceType': resource['ResourceType'],
                    'PhysicalResourceId': resource['PhysicalResourceId'],
                    'PropertyDifferences': resource['PropertyDifferences'] if 'PropertyDifferences' in resource else None
                })
            elif self.report_resources == REPORT_RESOURCES_NAMEONLY:
                resource_drifts[resource['StackResourceDriftStatus']].append(resource['LogicalResourceId'])
        return resource_drifts

    def wait_for_detection(self,backoff=3,max_tries=3):
        for detection in self.detections:
            try_count = 0
            detection_status = 'DETECTION_IN_PROGRESS'
            while detection_status == 'DETECTION_IN_PROGRESS' and try_count <= max_tries:
                print('Checking stack: {}, detection: {}'.format(detection['StackName'], detection['StackDriftDetectionId']))
                resp = self._cfn_call('describe_stack_drift_detection_status',{'StackDriftDetectionId':detection['StackDriftDetectionId']})
                detection_status = resp['DetectionStatus']
                if detection_status == 'DETECTION_IN_PROGRESS':
                    try_count += 1
                    sleep(backoff * try_count)
            # Detections fail if a resource is not supported but we want to know if there's a failure other than that.
            if detection_status == 'DETECTION_FAILED' and 'Failed to detect drift on resource' not in resp['DetectionStatusReason']:
                self.logger.log.critical('Detection Failed, StackId: {}, Reason: {}'.format(resp['StackId'],resp['DetectionStatusReason']))
                self.failed_stack_ids.append(resp['StackId'])

    def report(self):
        if self.report_aggregate:
            aggregate_report = {
                'Account': self.account,
                'Region': self.region,
                'Timestamp': self.start_time.isoformat(),
                'DRIFTED': [],
                'IN_SYNC': []
            }

        for stack in self._get_stacks():
            if stack['StackId'] not in self.failed_stack_ids:
                print('Stack result: {}'.format(stack))
                if stack['DriftInformation']['StackDriftStatus'] == 'DRIFTED':
                    if self.report_resources == REPORT_RESOURCES_NONE:
                        if self.report_aggregate:
                            aggregate_report["DRIFTED"].append(stack['StackName'],)
                        else:
                            stack_info = {
                                'Account': self.account,
                                'Region': self.region,
                                'Timestamp': self.start_time.isoformat(),
                                'StackName': stack['StackName']
                            }
                            self.logger.log.critical(json.dumps(stack_info, indent=2))
                    else:
                        resource_drifts = self._describe_resource_drifts(stack['StackName'])
                        if self.report_aggregate:
                            stack_info = {
                                'StackName': stack['StackName'],
                                'ResourceDrifts': resource_drifts
                            }
                            aggregate_report["DRIFTED"].append(stack_info)
                        else:
                            stack_info = {
                                'Account': self.account,
                                'Region': self.region,
                                'Timestamp': self.start_time.isoformat(),
                                'StackName': stack['StackName'],
                                'ResourceDrifts': resource_drifts
                            }
                            self.logger.log.critical(json.dumps(stack_info, indent=2))
                else:
                    if self.report_aggregate:
                        aggregate_report["IN_SYNC"].append(stack['StackName'])
                    else:
                        log_line = 'StackName: {}, DriftStatus: {}, LastCheckTimestamp: {}'.format(
                            stack['StackName'],
                            stack['DriftInformation']['StackDriftStatus'],
                            stack['DriftInformation']['LastCheckTimestamp']
                        )
                        self.logger.log.info(log_line)

        if self.report_aggregate:
            if len(aggregate_report['DRIFTED']) > 0:
                self.logger.log.critical(json.dumps(aggregate_report, indent=2))
            else:
                self.logger.log.info(json.dumps(aggregate_report, indent=2))

    @retry(ClientError)
    def _cfn_call(self,method,parameters={}):
        '''
        Put a retry decorator on any cfn method to avoid rate limit
        '''
        return getattr(self.cfn_client,method)(**parameters)

def lambda_handler(event,context):
    # Configure
    if isinstance(context,test_context):
        profile = context.profile
        regions = DEFAULT_REGIONS if context.regions == 'all' else context.regions.split(',')
    else:
        profile = None
        regions_str = os.environ.get('REGIONS')
        if len(regions_str) > 0:
            regions = regions_str.split(',')
        else:
            regions = DEFAULT_REGIONS

    sns_topic = os.environ.get('SNS_TOPIC_ID', None)
    sns_subject = os.environ.get('SNS_SUBJECT', DEFAULT_SNS_SUBJECT)
    logger = SNSlogger(sns_topic, sns_subject, profile=profile)

    last_check_threshold = int(os.environ.get('LAST_CHECK_THRESHOLD_SECS',DEFAULT_LAST_CHECK_THRESHOLD_SECS))
    report_aggregate = True if os.environ.get('REPORT_AGGREGATE', DEFAULT_REPORT_AGGREGATE).lower() == 'true' else False
    report_resources = os.environ.get('REPORT_RESOURCES', DEFAULT_REPORT_RESOURCES)
    if report_resources not in REPORT_RESOURCES_OPTIONS:
        report_resources = DEFAULT_REPORT_RESOURCES

    print(F"Initialized config (regions={regions}, sns_topic={sns_topic}, sns_subject={sns_subject}, last_check_threshold={last_check_threshold}, report_aggregate={report_aggregate}, report_resources={report_resources})")

    # Run the detection process
    for region in regions:
        DriftDetector(profile, region, logger, last_check_threshold, report_aggregate, report_resources)

class test_context(dict):
    '''This is a text context object used when running function locally'''
    def __init__(self,profile,regions):
        self.profile = profile
        self.regions = regions

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Lambda Function that detects CloudFormation drift.')
    parser.add_argument("-r","--regions", help="Optional regions in which to run. Default: all (will run for all regions)", default='all')
    parser.add_argument("-p","--profile", help="Optional profile name to use when connecting to aws. Default to local default or env var AWS_PROFILE", default=None)
    parser.add_argument("-s","--sns-topic-id", help="Optional SNS topic ARN. Default is to not send report to any sns topic.", default=None)
    parser.add_argument("-b","--sns-subject", help=F"Optional SNS subject. Default is {DEFAULT_SNS_SUBJECT}.", default=None)
    parser.add_argument("-l","--last-check-threshold-secs", help=F"Optional stack drift last checked in seconds. Default is {DEFAULT_LAST_CHECK_THRESHOLD_SECS}.", default=None)
    parser.add_argument("-a","--report-aggregate", help=F"True to report once for all stacks per region, False to report one per stack/region. In both cases, report only sent if there are drifts. Default is {DEFAULT_REPORT_AGGREGATE}.", default=None)
    parser.add_argument("-e","--report-resources", help=F"None suppresses listing of each drifted stack's drifted resources. NameOnly lists all modified and deleted resources in a drifted stack. Detailed adds more information on each resource drift. Default is {DEFAULT_REPORT_RESOURCES}.", default=None)

    args = parser.parse_args()

    event = {}
    context = test_context(args.profile,args.regions)
    if args.sns_topic_id:
        os.environ["SNS_TOPIC_ID"] = args.sns_topic_id
    if args.sns_subject:
        os.environ["SNS_SUBJECT"] = args.sns_subject
    if args.last_check_threshold_secs:
        os.environ["LAST_CHECK_THRESHOLD_SECS"] = args.last_check_threshold_secs
    if args.report_aggregate:
        os.environ["REPORT_AGGREGATE"] = args.report_aggregate
    if args.report_resources:
        os.environ["REPORT_RESOURCES"] = args.report_resources

    lambda_handler(event,context)
