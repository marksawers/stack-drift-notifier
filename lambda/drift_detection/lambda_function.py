#!/usr/bin/env python3

import copy, json, os
from datetime import datetime, timedelta, timezone
from time import sleep
import boto3
from botocore.exceptions import ClientError

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
REPORT_CHANNEL_SCOPE_FULL_SNS_NO_S3 = "FullSnsNoS3"
REPORT_CHANNEL_SCOPE_FULL_SNS_FULL_S3 = "FullSnsFullS3"
REPORT_CHANNEL_SCOPE_SUMMARY_SNS_FULL_S3 = "SummarySnsFullS3"
REPORT_CHANNEL_SCOPE_OPTIONS = [ REPORT_CHANNEL_SCOPE_FULL_SNS_NO_S3, REPORT_CHANNEL_SCOPE_FULL_SNS_FULL_S3, REPORT_CHANNEL_SCOPE_SUMMARY_SNS_FULL_S3 ]
DEFAULT_REPORT_CHANNEL_SCOPE = REPORT_CHANNEL_SCOPE_FULL_SNS_NO_S3
DEFAULT_SNS_SUBJECT = 'CFN Drift Detector Report'

class DriftDetector(object):
    '''
    DriftDetector is a class that runs drift detection on every stack, and,
    depending on the configuration, writes one or more reports to the logger.
    '''
    stacks = []
    failed_stack_ids = []

    def __init__(self,profile,account,region,logger,last_check_threshold,report_aggregate,
                report_resources, report_channel_scope, report_s3_bucket, report_s3_prefix):
        self.account = account
        self.region = region
        self.logger = logger
        self.report_aggregate = report_aggregate
        self.report_resources = report_resources
        self.report_channel_scope = report_channel_scope
        self.report_s3_bucket = report_s3_bucket
        self.report_s3_prefix = report_s3_prefix

        session = boto3.session.Session(
                      profile_name=profile,
                      region_name=region
                  )
        self.start_time_str = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
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


    def check_drift(self, last_check_threshold):
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


    def _detect(self, stack_name):
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
                self.logger.log.critical(e.response['Error']['Message'])
            return None

    def _describe_resource_drifts(self, stack_name):
        try:
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
        except Exception as error:
            return F'Error describing resources: {error}'

    def _save_report_to_s3(self, report, bucket, key):
        self.logger.log.info(F'Saving report to bucket={bucket}, key={key} ...')
        s3_local_client = boto3.Session().client('s3')
        content = json.dumps(report, indent=2)
        s3_local_client.put_object(Bucket=bucket, Key=key, Body=content)

    def wait_for_detection(self, backoff=3, max_tries=3):
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
            summary_aggregate_report = {
                'Account': self.account,
                'Region': self.region,
                'Timestamp': self.start_time_str,
                'DRIFTED': [],
                'IN_SYNC': []
            }
            full_aggregate_report = copy.deepcopy(summary_aggregate_report)

        if self.report_channel_scope != REPORT_CHANNEL_SCOPE_FULL_SNS_NO_S3:
            report_s3_base_key = F'{self.report_s3_prefix}/{self.account}/{self.region}'

        for stack in self._get_stacks():
            if stack['StackId'] not in self.failed_stack_ids:
                print('Stack result: {}'.format(stack))
                if stack['DriftInformation']['StackDriftStatus'] == 'DRIFTED':
                    if self.report_aggregate:
                        summary_aggregate_report['DRIFTED'].append(stack['StackName'])
                        if self.report_resources == REPORT_RESOURCES_NONE:
                            full_aggregate_report['DRIFTED'].append(stack['StackName'])
                        else:  # REPORT_RESOURCES_DETAILED or REPORT_RESOURCES_NAMEONLY
                            resource_drifts = self._describe_resource_drifts(stack['StackName'])
                            full_aggregate_report['DRIFTED'].append( {
                                    'StackName': stack['StackName'],
                                    'ResourceDrifts': resource_drifts
                                })
                    else: # not self.report_aggregate
                        summary_stack_info = {
                            'Account': self.account,
                            'Region': self.region,
                            'Timestamp': self.start_time_str,
                            'StackName': stack['StackName']
                        }
                        if self.report_resources == REPORT_RESOURCES_NONE:
                            full_stack_info = summary_stack_info
                        else: # DETAILED or NAMEONLY
                            full_stack_info = copy.deepcopy(summary_stack_info)
                            full_stack_info['ResourceDrifts'] = resource_drifts
                        # Save to s3
                        if self.report_channel_scope != REPORT_CHANNEL_SCOPE_FULL_SNS_NO_S3:
                            report_s3_key = F'{report_s3_base_key}/{stack["StackName"]}-{self.start_time_str}.json'
                            summary_stack_info["Report"] = F's3://{self.report_s3_bucket}/{report_s3_key}'
                            full_stack_info["Report"] = F's3://{self.report_s3_bucket}/{report_s3_key}'
                            self._save_report_to_s3(full_stack_info, self.report_s3_bucket, report_s3_key)
                        # Publish on sns
                        if self.report_channel_scope == REPORT_CHANNEL_SCOPE_SUMMARY_SNS_FULL_S3:
                            self.logger.log.critical(json.dumps(summary_stack_info, indent=2))
                        else: # FULL_SNS_NO_S3 or FULL_SNS_FULL_S3
                            self.logger.log.critical(json.dumps(full_stack_info, indent=2))

                        print(F"Summary is now {summary_aggregate_report['DRIFTED']}")
                else:   # IN_SYNC
                    if self.report_aggregate:
                        summary_aggregate_report["IN_SYNC"].append(stack['StackName'])
                        full_aggregate_report["IN_SYNC"].append(stack['StackName'])
                    else:
                        log_line = 'StackName: {}, DriftStatus: {}, LastCheckTimestamp: {}'.format(
                            stack['StackName'],
                            stack['DriftInformation']['StackDriftStatus'],
                            stack['DriftInformation']['LastCheckTimestamp']
                        )
                        self.logger.log.info(log_line)
        if self.report_aggregate:
            if len(summary_aggregate_report['DRIFTED']) > 0:
                # Save to s3
                if self.report_channel_scope != REPORT_CHANNEL_SCOPE_FULL_SNS_NO_S3:
                    report_s3_key = F'{report_s3_base_key}/{self.start_time_str}.json'
                    summary_aggregate_report["Report"] = F's3://{self.report_s3_bucket}/{report_s3_key}'
                    full_aggregate_report["Report"] = F's3://{self.report_s3_bucket}/{report_s3_key}'
                    self._save_report_to_s3(full_aggregate_report, self.report_s3_bucket, report_s3_key)
                # Publish on sns
                if self.report_channel_scope == REPORT_CHANNEL_SCOPE_SUMMARY_SNS_FULL_S3:
                    self.logger.log.critical(json.dumps(summary_aggregate_report, indent=2))
                else: # FULL_SNS_NO_S3 or FULL_SNS_FULL_S3
                    self.logger.log.critical(json.dumps(full_aggregate_report, indent=2))
            else:
                self.logger.log.info(json.dumps(full_aggregate_report, indent=2))

    @retry(ClientError)
    def _cfn_call(self,method,parameters={}):
        '''
        Put a retry decorator on any cfn method to avoid rate limit
        '''
        return getattr(self.cfn_client,method)(**parameters)

def lambda_handler(event, context):
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
    account = boto3.session.Session(profile_name=profile).client('sts').get_caller_identity()['Account']

    sns_topic = os.environ.get('SNS_TOPIC_ID', None)
    sns_subject = os.environ.get('SNS_SUBJECT', DEFAULT_SNS_SUBJECT)
    logger = SNSlogger(sns_topic, 'placeholder', profile=profile)

    last_check_threshold = int(os.environ.get('LAST_CHECK_THRESHOLD_SECS',DEFAULT_LAST_CHECK_THRESHOLD_SECS))
    report_aggregate = True if os.environ.get('REPORT_AGGREGATE', DEFAULT_REPORT_AGGREGATE).lower() == 'true' else False
    report_resources = os.environ.get('REPORT_RESOURCES', DEFAULT_REPORT_RESOURCES)
    if report_resources not in REPORT_RESOURCES_OPTIONS:
        print(F'WARNING: Overriding invalid REPORT_RESOURCES {report_resources} to {DEFAULT_REPORT_RESOURCES}')
        report_resources = DEFAULT_REPORT_RESOURCES
    report_channel_scope = os.environ.get('REPORT_CHANNEL_SCOPE', DEFAULT_REPORT_CHANNEL_SCOPE)
    if report_channel_scope not in REPORT_CHANNEL_SCOPE_OPTIONS:
        print(F'WARNING: Overriding invalid REPORT_CHANNEL_SCOPE {report_channel_scope} to {DEFAULT_REPORT_CHANNEL_SCOPE}')
        report_channel_scope = DEFAULT_REPORT_CHANNEL_SCOPE
    report_s3_bucket = os.environ.get('REPORT_S3_BUCKET', None)
    report_s3_prefix = os.environ.get('REPORT_S3_PREFIX', None)
    if report_channel_scope != REPORT_CHANNEL_SCOPE_FULL_SNS_NO_S3 and (not report_s3_bucket or not report_s3_prefix):
        print(F'WARNING: Overriding REPORT_CHANNEL_SCOPE {report_channel_scope} to {DEFAULT_REPORT_CHANNEL_SCOPE} since REPORT_S3_BUCKET OR REPORT_S3_PREFIX is null')
        report_channel_scope = DEFAULT_REPORT_CHANNEL_SCOPE
    print(F'Initialized config (account={account}, regions={regions}, sns_topic={sns_topic}, sns_subject={sns_subject}, last_check_threshold={last_check_threshold}, report_aggregate={report_aggregate}, report_resources={report_resources}, report_channel_scope={report_channel_scope}, report_s3_bucket={report_s3_bucket}, report_s3_prefix={report_s3_prefix})')

    # Run the detection process
    for region in regions:
        try:
            specified_sns_subject = sns_subject % { 'account': account, 'region': region }
        except:
            print(F'WARNING: Invalid syntax in sns subject {sns_subject}. Cannot substitute account and region.')
            specified_sns_subject = sns_subject
        logger.subject = specified_sns_subject
        DriftDetector(profile, account, region, logger, last_check_threshold, report_aggregate, report_resources, report_channel_scope, report_s3_bucket, report_s3_prefix)

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
    parser.add_argument("-c","--report-channel-scope", help=F"Sets the scope of reporting for each channel. Default is {DEFAULT_REPORT_CHANNEL_SCOPE}.", default=None)
    parser.add_argument("-u","--report-s3-bucket", help="Optional bucket name to store drift reports", default=None)
    parser.add_argument("-f","--report-s3-prefix", help="Optional directory path to store drift reports, e.g. reports/stack-drifts", default=None)

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
    if args.report_channel_scope:
        os.environ["REPORT_CHANNEL_SCOPE"] = args.report_channel_scope
    if args.report_s3_bucket:
        os.environ["REPORT_S3_BUCKET"] = args.report_s3_bucket
    if args.report_s3_prefix:
        os.environ["REPORT_S3_PREFIX"] = args.report_s3_prefix

    lambda_handler(event,context)
