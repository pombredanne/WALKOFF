import json
import logging
import os
import signal
import threading

import zmq
import zmq.auth as auth
from zmq.utils.strtypes import cast_unicode

import core.config.config
import core.config.paths
from core.argument import Argument
from core.events import EventType, WalkoffEvent
from core.executionelements.workflow import Workflow
from core.protobuf.build import data_pb2

try:
    from Queue import Queue
except ImportError:
    from queue import Queue


REQUESTS_ADDR = 'tcp://127.0.0.1:5555'
RESULTS_ADDR = 'tcp://127.0.0.1:5556'
COMM_ADDR = 'tcp://127.0.0.1:5557'

logger = logging.getLogger(__name__)


def recreate_workflow(workflow_json):
    """Recreates a workflow from a JSON to prepare for it to be executed.

    Args:
        workflow_json (JSON dict): The input workflow JSON, with some other fields as well.

    Returns:
        (Workflow object, start_arguments): A tuple containing the reconstructed Workflow object, and the arguments to
            the start action.
    """
    uid = workflow_json['uid']
    del workflow_json['uid']
    execution_uid = workflow_json['execution_uid']
    del workflow_json['execution_uid']
    start = workflow_json['start']

    start_arguments = {}
    if 'start_arguments' in workflow_json:
        start_arguments = [Argument(**arg) for arg in workflow_json['start_arguments']]
        workflow_json.pop("start_arguments")

    workflow = Workflow.create(workflow_json)
    workflow.uid = uid
    workflow.set_execution_uid(execution_uid)
    workflow.start = start

    return workflow, start_arguments


def convert_to_protobuf(sender, workflow_execution_uid='', **kwargs):
    """Converts an execution element and its data to a protobuf message.

    Args:
        sender (execution element): The execution element object that is sending the data.
        workflow_execution_uid (str, optional): The execution UID of the Workflow under which this execution
            element falls. It is not required and defaults to an empty string, but it is highly recommended
            so that the LoadBalancer can keep track of the Workflow's execution.
        kwargs (dict, optional): A dict of extra fields, such as data, callback_name, etc.

    Returns:
        The newly formed protobuf object, serialized as a string to send over the ZMQ socket.
    """
    event = kwargs['event']
    packet = data_pb2.Message()
    if event.event_type == EventType.workflow:
        if 'data' in kwargs:
            packet.type = data_pb2.Message.WORKFLOWPACKETDATA
            wf_packet = packet.workflow_packet_data
            wf_packet.additional_data = json.dumps(kwargs['data'])
        else:
            packet.type = data_pb2.Message.WORKFLOWPACKET
            wf_packet = packet.workflow_packet
        wf_packet.sender.name = sender.name
        wf_packet.sender.uid = sender.uid
        wf_packet.sender.workflow_execution_uid = workflow_execution_uid
        wf_packet.callback_name = event.name
    elif event.event_type == EventType.action:
        if 'data' in kwargs:
            packet.type = data_pb2.Message.ACTIONPACKETDATA
            action_packet = packet.action_packet_data
            action_packet.additional_data = json.dumps(kwargs['data'])
        else:
            packet.type = data_pb2.Message.ACTIONPACKET
            action_packet = packet.action_packet
        action_packet.sender.name = sender.name
        action_packet.sender.uid = sender.uid
        action_packet.sender.workflow_execution_uid = workflow_execution_uid
        action_packet.sender.execution_uid = sender.get_execution_uid()
        action_packet.sender.app_name = sender.app_name
        action_packet.sender.action_name = sender.action_name
        action_packet.sender.device_id = sender.device_id if sender.device_id is not None else -1

        for argument in sender.arguments.values():
            arg = action_packet.sender.arguments.add()
            arg.name = argument.name
            for field in ('value', 'reference', 'selection'):
                val = getattr(argument, field)
                if val is not None:
                    setattr(arg, field, str(val))

        action_packet.callback_name = event.name

    elif event.event_type in (EventType.branch, EventType.condition, EventType.transform):
        packet.type = data_pb2.Message.GENERALPACKET
        general_packet = packet.general_packet
        general_packet.sender.uid = sender.uid
        general_packet.sender.workflow_execution_uid = workflow_execution_uid
        if hasattr(sender, 'app_name'):
            general_packet.sender.app_name = sender.app_name
        general_packet.callback_name = event.name
    packet_bytes = packet.SerializeToString()
    return packet_bytes


class Worker:
    def __init__(self, id_, worker_environment_setup=None):
        """Initialize a Workflow object, which will be executing workflows.

        Args:
            id_ (str): The ID of the worker. Needed for ZMQ socket communication.
            worker_environment_setup (func, optional): Function to setup globals in the worker.
        """
        signal.signal(signal.SIGINT, self.exit_handler)
        signal.signal(signal.SIGABRT, self.exit_handler)

        @WalkoffEvent.CommonWorkflowSignal.connect
        def handle_data_sent(sender, **kwargs):
            self.on_data_sent(sender, **kwargs)

        self.handle_data_sent = handle_data_sent

        self.thread_exit = False
        self.workflow = None

        server_secret_file = os.path.join(core.config.paths.zmq_private_keys_path, "server.key_secret")
        server_public, server_secret = auth.load_certificate(server_secret_file)
        client_secret_file = os.path.join(core.config.paths.zmq_private_keys_path, "client.key_secret")
        client_public, client_secret = auth.load_certificate(client_secret_file)

        self.ctx = zmq.Context()

        self.request_sock = self.ctx.socket(zmq.REQ)
        self.request_sock.identity = u"Worker-{}".format(id_).encode("ascii")
        self.request_sock.curve_secretkey = client_secret
        self.request_sock.curve_publickey = client_public
        self.request_sock.curve_serverkey = server_public
        self.request_sock.connect(REQUESTS_ADDR)

        self.comm_sock = self.ctx.socket(zmq.REQ)
        self.comm_sock.identity = u"Worker-{}".format(id_).encode("ascii")
        self.comm_sock.curve_secretkey = client_secret
        self.comm_sock.curve_publickey = client_public
        self.comm_sock.curve_serverkey = server_public
        self.comm_sock.connect(COMM_ADDR)

        self.results_sock = self.ctx.socket(zmq.PUSH)
        self.results_sock.identity = u"Worker-{}".format(id_).encode("ascii")
        self.results_sock.curve_secretkey = client_secret
        self.results_sock.curve_publickey = client_public
        self.results_sock.curve_serverkey = server_public
        self.results_sock.connect(RESULTS_ADDR)

        if worker_environment_setup:
            worker_environment_setup()
        else:
            core.config.config.initialize()

        self.comm_thread = threading.Thread(target=self.receive_data)
        self.comm_thread.start()

        self.execute_workflow_worker()

    def exit_handler(self, signum, frame):
        """Clean up upon receiving a SIGINT or SIGABT.
        """
        self.thread_exit = True
        if self.comm_thread:
            self.comm_thread.join(timeout=2)
        if self.request_sock:
            self.request_sock.close()
        if self.results_sock:
            self.results_sock.close()
        if self.comm_sock:
            self.comm_sock.close()
        os._exit(0)

    def execute_workflow_worker(self):
        """Keep executing workflows as they come in over the ZMQ socket from the manager.
        """
        self.request_sock.send(b"Ready")
        self.comm_sock.send(b"Executing")

        while True:
            workflow_in = self.request_sock.recv()

            self.workflow, start_arguments = recreate_workflow(json.loads(cast_unicode(workflow_in)))

            self.workflow.execute(execution_uid=self.workflow.get_execution_uid(), start=self.workflow.start,
                                  start_arguments=start_arguments)
            self.request_sock.send(b"Done")

    def receive_data(self):
        """Constantly receives data from the ZMQ socket and handles it accordingly.
        """
        while True:
            if self.thread_exit:
                break
            try:
                message = self.comm_sock.recv()
            except zmq.ZMQError:
                continue

            if message == b'Exit':
                break
            if message == b'Pause':
                self.workflow.pause()
                self.comm_sock.send(b"Paused")
            elif message == b'Resume':
                self.workflow.resume()
                self.comm_sock.send(b"Resumed")
            else:
                decoded_message = json.loads(message.decode("utf-8"))
                if "arguments" in decoded_message:
                    arguments = []
                    for arg in decoded_message["arguments"]:
                        arguments.append(Argument(**arg))
                    decoded_message["arguments"] = arguments
                self.workflow.send_data_to_action(decoded_message)
                self.comm_sock.send(b"Received")

        return

    def on_data_sent(self, sender, **kwargs):
        """Listens for the data_sent callback, which signifies that an execution element needs to trigger a
                callback in the main thread.

            Args:
                sender (execution element): The execution element that sent the signal.
                kwargs (dict): Any extra data to send.
        """
        packet_bytes = convert_to_protobuf(sender, self.workflow.get_execution_uid(), **kwargs)
        self.results_sock.send(packet_bytes)
