import boto3
import logging
import re

class SNSLogHandler(logging.Handler):
    def __init__(self, topic, subject, profile=None):
        logging.Handler.__init__(self)
        search = re.search('arn:[a-zA-Z0-9_-]+:\w+:(.+?):\d+:.+$', topic)
        if search:
            region = search.group(1)
        else:
            region = None
            print(F'ERROR: sns topic id {topic} not expected. It should be an ARN!' )
        self.session = boto3.session.Session(profile_name=profile,region_name=region)
        self.sns_client = self.session.client('sns')
        self.topic = topic
        self.subject = subject

    def emit(self, record):
        self.sns_client.publish(TopicArn=self.topic, Message=record.message, Subject=self.subject)

class SNSlogger(object):
    def __init__(self, sns_topic_id, sns_topic_subject, profile=None):
        self.snsLogHandler = None
        self.sns_topic = sns_topic_id
        self.subject = sns_topic_subject
        self.profile = profile
        self._init_logging()

    def _init_logging(self):
        self.log = logging.getLogger('SNS_Logger')

        # Should set the level on the logger itself to DEBUG
        # and let the handlers below do the filtering
        self.log.setLevel(logging.DEBUG)
        # Setting console output to DEBUG for easier debugging
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        # THIS NEEDS TO BE CHANGED.
        hdlr = logging.FileHandler('/tmp/sns.log')
        hdlr.setFormatter(formatter)
        ch.setFormatter(formatter)
        self.log.addHandler(ch)
        self.log.addHandler(hdlr)
        if self.sns_topic and self.sns_subject:
            self.snsLogHandler = SNSLogHandler(self.sns_topic, self.sns_subject, self.profile)

            # We only want critical messages bothering us via AWS SNS
            self.snsLogHandler.setLevel(logging.CRITICAL)
            self.snsLogHandler.setFormatter(formatter)
            self.log.addHandler(self.snsLogHandler)

    @property
    def subject(self):
        return self.sns_subject

    @subject.setter
    def subject(self, subject):
        self.sns_subject = subject
        if self.snsLogHandler:
            self.snsLogHandler.subject = subject
