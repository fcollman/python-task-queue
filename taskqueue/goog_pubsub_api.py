import json
import re
import types

from google.cloud import pubsub_v1
from google.api_core.exceptions import ClientError

from .lib import toiter, sip, jsonify


import tenacity

PUBSUB_BATCH_SIZE = 10  # send_message_batch's max batch size is 10


class ClientSideError(Exception):
    pass


retry = tenacity.retry(
    reraise=True,
    stop=tenacity.stop_after_attempt(4),
    wait=tenacity.wait_random_exponential(0.5, 60.0),
    retry=tenacity.retry_if_not_exception_type(ClientSideError),
)


class PubSubTaskQueueAPI(object):
    def __init__(self, qurl, **kwargs):
        """
        qurl: a topic or subscription location
          like projects/{project_id}/topics/{topic_id}/subscriptions/{subscription_id}
        kwargs: Keywords for the underlying boto3.client constructor, other than `service_name`,
          `region_name`, `aws_secret_access_key`, or `aws_access_key_id`.
        """

        matches = re.search(r"projects/([\w\d-]+)/", qurl)
        if matches is not None:
            self.project_id = matches.group(1)
        else:
            raise ValueError("Project ID not found in qurl")

        matches = re.search(r"topics/([\w\d-]+)", qurl)
        if matches is not None:
            self.topic_id = matches.group(1)
        else:
            self.topic_id = ValueError("topic_id not found in qurl")

        matches = re.search(r"subscriptions/([\w\d-]+)", qurl)
        if matches is not None:
            self.subscription_id = matches.group(1)
        else:
            self.subscription_id = ValueError("subscription_id not found in qurl")

        self.subscriber = pubsub_v1.SubscriberClient()
        self.publisher = pubsub_v1.PublisherClient()
        self._topic_path = self.publisher.topic_path(
            self.project_id, self.subscription_id
        )
        self._subscription_path = self.subscriber.subscription_path(
            self.project_id, self.subscription_id
        )

        self.batch_size = PUBSUB_BATCH_SIZE

    @property
    def enqueued(self):
        raise float("Nan")

    @property
    def inserted(self):
        return float("NaN")

    @property
    def completed(self):
        return float("NaN")

    @property
    def leased(self):
        return float("NaN")

    def is_empty(self):
        return self.enqueued == 0

    @retry
    def insert(self, tasks, delay_seconds=0):
        tasks = toiter(tasks)

        def publish_batch(batch):
            if not batch:
                return 0

            futures = []
            for task in batch:
                data = jsonify(task).encode("utf-8")
                future = self.publisher.publish(self._topic_path, data)
                futures.append(future)

            # Wait for all messages to be published
            for future in futures:
                try:
                    # Blocks until the message is published
                    future.result()
                except Exception as e:
                    raise ClientError(e)

            return len(futures)

        total = 0

        # send_message_batch's max batch size is 10
        for batch in sip(tasks, self.batch_size):
            if len(batch) == 0:
                break
            total += publish_batch(batch)

        return total

    def add_insert_count(self, ct):
        pass

    def rezero(self):
        pass

    @retry
    def renew_lease(self, task, seconds):
        self.subscriber.modify_ack_deadline(
            self._subscription_path,
            [task.id],
            seconds,
        )

    def cancel_lease(self, task):
        self.subscriber.acknowledge(self._subscription_path, [task.id])

    def release_all(self):
        raise NotImplementedError()

    def lease(self, seconds, num_tasks=1, wait_sec=20):
        # Pull messages from the subscription
        response = self.subscriber.pull(self.subscription_path, max_messages=num_tasks)

        tasks = []
        for received_message in response.received_messages:
            # Load the message data as JSON
            task = json.loads(received_message.message.data.decode("utf-8"))
            # Store the acknowledgement ID in the task
            task["id"] = received_message.ack_id
            tasks.append(task)

        return tasks

    def delete(self, task):
        if isinstance(task, str):
            ack_id = task
        else:
            try:
                ack_id = task._id
            except AttributeError:
                ack_id = task["id"]

        self.subscriber.acknowledge(self._subscription_path, [ack_id])

        return 1

    def tally(self):
        pass

    def purge(self, native=False):
        while True:
            # Pull messages from the subscription
            response = self.subscriber.pull(
                self._subscription_path, max_messages=self.batch_size
            )

            if not response.received_messages:
                # No more messages, break the loop
                break

            # Acknowledge all received messages
            ack_ids = [msg.ack_id for msg in response.received_messages]
            self.subscriber.acknowledge(self._subscription_path, ack_ids)

    def __iter__(self):
        return iter(self.lease(num_tasks=10, seconds=0))
