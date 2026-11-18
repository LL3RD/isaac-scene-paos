"""Thin Dora binding for the pinned Forge ToolEndpoint SDK; no scene logic here."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import uuid

from forge_tool import (EndpointStatus, ToolEndpointDescriptor, ToolEndpointHandler,
                        ToolEndpointError, ToolOperationDescriptor, make_endpoint_status_envelope,
                        make_registration_envelope, make_unregister_envelope,
                        invoke_request_from_envelope, make_error_response_envelope)
from forge_tool.dora import DoraToolEndpointBinding, tool_envelope_to_message, tool_message_to_envelope
from contracts import ENDPOINT, PROTOCOL, QUERIES, SCHEMAS, tool_specs
from service import Operation, SceneService


def make_handler(service):
    descriptor = ToolEndpointDescriptor(protocol_version=PROTOCOL, endpoint_id=ENDPOINT,
        operations=tuple(ToolOperationDescriptor(name=op, semantics="query" if op in QUERIES else "action",
            cancellable=op not in QUERIES, status_supported=op not in QUERIES,
            max_concurrency=8) for op in SCHEMAS))
    return ToolEndpointHandler(descriptor, endpoint_instance_id=uuid.uuid4().hex,
        operations={op: Operation(service, op) for op in SCHEMAS})


class SceneBinding(DoraToolEndpointBinding):
    """Check durable argument identity before the SDK's in-memory replay cache."""
    def __init__(self, handler, service, **kwargs):
        super().__init__(handler, **kwargs)
        self.service = service

    async def dispatch_input(self, value):
        request = self._request_from_arrow(value)
        if request.message_type == "tool.invoke.request" and request.operation not in QUERIES:
            arguments, context = invoke_request_from_envelope(request)
            try:
                self.service.check_identity(request.operation, arguments.arguments, context.execution_key)
            except ToolEndpointError as exc:
                return (tool_envelope_to_message(make_error_response_envelope(exc.error, request)).to_arrow(),)
        return await super().dispatch_input(value)


async def run(config):
    from dora import Node
    from forge_msgs import ToolMessage
    os.umask(0o077)
    service = SceneService(config)
    handler = make_handler(service)
    node = Node()

    async def publish(batch):
        node.send_output("to_gateway", batch)

    def announce():
        registration = make_registration_envelope(handler.descriptor,
            endpoint_instance_id=handler.endpoint_instance_id, request_id=uuid.uuid4().hex)
        node.send_output("to_gateway", tool_envelope_to_message(registration).to_arrow())
        status = make_endpoint_status_envelope(EndpointStatus(endpoint_id=ENDPOINT,
            state="ready", active_invocations=len(service.tasks), details={"delivery_scope": "review_scene"}),
            endpoint_instance_id=handler.endpoint_instance_id)
        node.send_output("to_gateway", tool_envelope_to_message(status).to_arrow())

    binding = SceneBinding(handler, service, event_sink=publish)
    try:
        announce()
        while True:
            event = await asyncio.to_thread(node.next)
            if event is None or event["type"] == "STOP":
                break
            if event["type"] != "INPUT":
                continue
            if event["id"] == "heartbeat":
                announce()
                continue
            envelope = tool_message_to_envelope(ToolMessage.from_arrow(event["value"]))
            if envelope.message_type == "endpoint.registry.response":
                if envelope.payload["status"] != "accepted":
                    raise RuntimeError("endpoint registration rejected")
                continue
            for batch in await binding.dispatch_input(event["value"]):
                await publish(batch)
    finally:
        await service.close()
        envelope = make_unregister_envelope(endpoint_id=ENDPOINT,
            endpoint_instance_id=handler.endpoint_instance_id, request_id=uuid.uuid4().hex)
        node.send_output("to_gateway", tool_envelope_to_message(envelope).to_arrow())


def write_profile(args):
    """Operator-only source profile generator, not a published artifact manifest."""
    directory = args.write_profile.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    # Dora 0.4.1 canonicalizes executable symlinks, which would bypass a venv.
    # A regular launcher preserves the configured interpreter's venv identity.
    interpreter = str(args.python.absolute())
    launcher = directory / "launch.py"
    launcher.write_text(f"#!{interpreter}\nimport os, sys\nos.execv({interpreter!r}, [{interpreter!r}, *sys.argv[1:]])\n")
    launcher.chmod(0o700)
    (directory / "zenoh.json").write_text(json.dumps({
        "listen": {"endpoints": ["tcp/127.0.0.1:0"]},
        "scouting": {"multicast": {"enabled": False}, "gossip": {"enabled": False}}}, indent=2))
    config = {"host": "127.0.0.1", "port": args.port, "joint_order": [],
        "tools": {"enabled": True, "invoke_timeout_ms": 900000, "lease_ttl_ms": 15000,
            "providers": [{"endpoint_id": ENDPOINT, "input_id": "provider/to_gateway", "output_id": "to_provider"}],
            "specs": tool_specs()}}
    (directory / "gateway.yaml").write_text(json.dumps(config, indent=2))
    source = Path(__file__).resolve()
    dataflow = {"nodes": [
        {"id": "gateway", "path": str(launcher), "args": shlex.join([str(args.gateway.resolve()),
            "--config", str(directory / "gateway.yaml")]),
            "inputs": {"provider/to_gateway": {"source": "provider/to_gateway", "queue_size": 128}},
            "outputs": ["to_provider"]},
        {"id": "provider", "path": str(launcher), "args": shlex.join([str(source), "--config", str(args.config.resolve())]),
            "inputs": {"from_gateway": {"source": "gateway/to_provider", "queue_size": 128},
                       "heartbeat": "dora/timer/millis/2000"},
            "outputs": ["to_gateway"]}]}
    (directory / "dataflow.yaml").write_text(json.dumps(dataflow, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=os.environ.get("ISAAC_SCENE_DEPLOYMENT"))
    parser.add_argument("--write-profile", type=Path)
    parser.add_argument("--gateway", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--port", type=int, default=19042)
    args = parser.parse_args()
    if args.config is None:
        parser.error("set ISAAC_SCENE_DEPLOYMENT or pass --config (operator configuration)")
    if args.write_profile:
        if not args.gateway or not args.python:
            parser.error("profile generation requires --gateway and --python")
        write_profile(args)
    else:
        asyncio.run(run(args.config))
