# CloudFormation Stack Drift Notifier

## Overview
The purpose of this project to setup a lambda that runs on a schedule to detect CloudFormation drift. The lambda runs on a schedule specified by a parameter passed to the CloudFormation stack that sets up the project. By default, the lambda will check every region in parallel. An SNS notification is sent to the subscribing email address per account for all stacks that have drifted, or if desired, one notification per drifted stack.

## Quick Setup
I host the lambda and the CloudFormation from a public bucket. You can launch it directly from this button. The lambda function package is distributed to a bucket in each region, which means that you can launch this template into any region you wish.

[![CloudFormation Link](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/new?stackName=Stack-Drift-Notifier&templateURL=https://s3.amazonaws.com/stack-drift-notifier/master/cloudformation/drift_detection.yaml)

## Manual Set up Drift Detector
Before you deploy this CloudFormation template, you need to build the lambda function into a zip and host it on S3.

### Assumptions:
* You have an AWS Account.
* You’re using Bash.
* You have git installed.
* You have pip installed. [Help](https://pip.pypa.io/en/stable/installing/)
* You have the AWS CLI installed, preferred version 1.16.54 or greater. [Help](https://docs.aws.amazon.com/cli/latest/userguide/installing.html)
* You have configured the CLI, set up AWS IAM access credentials that have appropreate access. [Help](https://docs.aws.amazon.com/cli/latest/reference/configure/index.html)

### Step 1: Clone the example Github project.
I have prepared a Github project with all of the example CloudFormation and code to get you off the ground. Clone this Github project to your local machine.

```
git clone https://github.com/dejonghe/stack-drift-notifier
```

### Step 2: Create an S3 Bucket. (Skip if you have a bucket to work from, use that buckets name from now on)
You will need an S3 bucket to work out of, we will use this bucket to upload our lambda code zip. Create the bucket with the following CLI command or through the console. Keep in mind that S3 bucket names are globally unique and you will have to come up with a bucket name for yourself. For example:
```
aws s3 mb s3://drift_detector_{yourUniqueId} (--profile optionalProfile)
```

### Step 3: Run the env_prep Script.
To prepare, you must run a script from within the Github project. This script is to be ran from the base of the repository. If you rename the repository directory you will need to edit the [script](./bin/env_prep.sh), all the variables are at the top.

This script performs the following tasks:
* Builds and and uploads the lambda code
* The script creates a temp directory
* Copies the code from [lambda/drift_detection](./lambda/drift_detection/) to the temp directory
* Zips up the contents of the temp directory to a package named ./lambda/drift_detection.zip
* Removes the temp directory
* Uploads the zip to `s3://{yourBucket}/{release(master)}/lambda/{lambdaName}.zip` where lambdName is drift_detection by default

To run the script withe defaults, provide your bucket name:
```
./bin/env_prep.sh -b drift_detector_{yourUniqueId}
```
The full options are:
```
Usage: bin/env_prep.sh {args}
Where valid args are:
  -b <bucket> (REQUIRED) -- bucket name to sync to
  -p <profile> -- Profile to use for AWS commands, defaults to 'default'
  -r <release> -- Release variable used for bucket path, defaults to 'master'
  -f <lambda-func-name> -- Lambda function name, defaults to 'drift_detection'
```

### Step 4: Create the CloudFormation Stack.
This step utilizes the [CloudFormation Tempalte](./cloudformation/drift_detection.yaml) to produce a number of resources that runs drift detection on a schedule. The template creates a IAM role for lambda to assume, a policy to go with it, a SNS topic to notify if a stack has drifted, the lambda function, a CloudWatch schedule, and permission for the schedule to invoke the lambda.

The basic invocation with all defaults looks like this:
```
aws cloudformation create-stack --template-body file://cloudformation/drift_detection.yaml --stack-name drift-detection --parameters '[{"ParameterKey":"NotifyEmail","ParameterValue":"EmailAddressToNotify"},{"ParameterKey":"LambdaS3Bucket","ParameterValue":"drift_detector_{yourUniqueId}"}]' --capabilities CAPABILITY_NAMED_IAM (--profile optionalProfile)
```
Wait for the CloudFormation stack to complete. With the default configuration, drift detection will run daily.

The template parameters are as follows:
  * LambdaFunctionName: Name lambda function. Default: drift_detection
  * LambdaS3Bucket: Name of S3 bucket containing lambda function zip. **REQUIRED**
  * LambdaS3Key: S3 key path of lambda function zip. Default: master/lambda/drift_detection.zip
  * Enabled: Enabled or Disable Drift Detector. Default: ENABLED
  * LambdaTimeout: Timeout for the lambda in seconds. Default: 300
  * SnsTopicName: SNS topic name. Default: Drift-Detector
  * NotifyEmail: Email address to notify for detected drift. **REQUIRED**
  * NotificationSubject: Subject of the Email notification. Default: CFN Drift Detector Report
  * ScheduleExpression: CloudWatch Event Rule Schedule Expression. Default: rate(1 day)
  * Regions: Comma-separated list of regions in scope. Default: 'ap-south-1,eu-west-3,eu-west-2,eu-west-1,ap-northeast-2,ap-northeast-1,sa-east-1,ca-central-1,ap-southeast-1,ap-southeast-2,eu-central-1,us-east-1,us-east-2,us-west-1,us-west-2'
  * LastCheckThresholdSecs: Age allowed of the last drift detection for each stack in seconds. Default: 60
  * ReportAggregate: True to report once for all stacks per region, False to report one per stack/region. In both cases, report only sent if there are drifts. Default: True
  * ReportResources: None suppresses listing of each drifted stack's drifted resources. NameOnly lists all modified and deleted resources in a drifted stack. Detailed adds more information on each resource drift. Default: NameOnly
