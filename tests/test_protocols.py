import os
import pwd
import sys

import pytest

from theo.backends.acp import ACPBackend
from theo.backends.claude import ClaudeBackend, claude_terminal
from theo.backends.codex import CodexBackend
from theo.backends.policy import Accounts, inspect_configuration, worker_environment
from theo.domain import AuthWait, Denied, ExecutionRequest, Outcome, QuotaWait


@pytest.mark.parametrize(
    "packet,code,expected",
    [
        ({"type": "result", "subtype": "success", "result": "hello"}, 0, Outcome.COMPLETED),
        (
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "errors": ["authentication expired"],
            },
            0,
            Outcome.AUTH,
        ),
        ({"type": "result", "subtype": "error", "errors": ["rate_limit"]}, 0, Outcome.QUOTA),
        ({"type": "result", "subtype": "success"}, 1, Outcome.FAILED),
        ({}, 0, Outcome.FAILED),
    ],
)
def test_a11_structured_terminal_never_invents_success_or_usage(packet, code, expected):
    outcome = claude_terminal(packet, code)
    assert outcome.status == expected
    assert outcome.input_tokens is None


@pytest.mark.parametrize(
    "key",
    [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "XAI_API_KEY",
        "CURSOR_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "NODE_OPTIONS",
        "AWS_SECRET_ACCESS_KEY",
    ],
)
def test_a12_no_paid_env_or_secret_logging(key, tmp_path):
    with pytest.raises(Denied) as error:
        worker_environment(tmp_path, {key: "secret-sentinel-value"})
    assert "secret-sentinel-value" not in str(error.value)


def test_a12_custom_provider_configuration_denied(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('base_url="https://paid-route.example"')
    with pytest.raises(Denied):
        inspect_configuration([config])


def test_worker_environment_has_os_account_identity_without_inheriting_secrets(tmp_path):
    identity = pwd.getpwuid(os.geteuid()).pw_name
    env = worker_environment(tmp_path, {"USER": "wrong", "LOGNAME": "wrong", "SECRET": "private"})
    assert env["USER"] == env["LOGNAME"] == identity
    assert env["HOME"] == str(tmp_path)
    assert "SECRET" not in env


def test_worker_environment_uses_dedicated_runner_identity(tmp_path, monkeypatch):
    from types import SimpleNamespace

    def identity(uid):
        assert uid == 12345
        return SimpleNamespace(pw_name="theo-runner")

    monkeypatch.setattr("theo.backends.policy.pwd.getpwuid", identity)
    env = worker_environment(tmp_path, {}, runner_uid=12345)
    assert env["USER"] == env["LOGNAME"] == "theo-runner"


async def test_a13_a14_a39_shared_pool_eligibility_and_version_invalidation(db, clock):
    accounts = Accounts(db, "owner")
    evidence = {
        "account_ref": "owner-native",
        "label": "Test fixture",
        "pool_id": "shared-pool",
        "models": ["model-one", "model-two"],
        "runtime_version": "fixture-1",
        "fingerprint": "fingerprint",
        "config_hash": "config",
        "verification_method": "native_and_operator_attestation",
        "native_subscription_login": True,
        "extra_usage_disabled": True,
        "hard_stop_verified": True,
        "evidence": "synthetic hard-stop fixture",
    }
    await accounts.register("claude", evidence)
    account = await accounts.eligible("claude", "model-one", "fingerprint", "config")
    with pytest.raises(AuthWait):
        await accounts.eligible("claude", "model-one", "new-version", "config")
    await accounts.exhaust(account)
    with pytest.raises(QuotaWait):
        await accounts.eligible("claude", "model-two", "fingerprint", "config")
    usage = await accounts.usage()
    assert usage["remaining_allowance"] is None


PROTOCOL_SERVER = r"""
import sys,json
def send(x): print(json.dumps(x),flush=True)
args=sys.argv[1:]
if "--version" in args:
 print("fixture-1");sys.exit(0)
if "--print" in args:
 assert args[args.index("--tools")+1]==""
 assert "--strict-mcp-config" in args
 assert json.loads(args[args.index("--settings")+1])["autoMemoryEnabled"] is False
 assert "remember and recall tools" in args[args.index("--system-prompt")+1]
 assert "Host persona fixture" in args[args.index("--system-prompt")+1]
 prompt=sys.stdin.read()
 send({"type":"system","subtype":"init","model":"fixture-model"})
 send({"type":"assistant","message":{"content":[{"type":"text","text":"Fixture answer"}]}})
 send({"type":"result","subtype":"success","result":"Fixture answer"});sys.exit(0)
for line in sys.stdin:
 p=json.loads(line);m=p.get("method");params=p.get("params",{});result={}
 if m=="initialize":
  result={"protocolVersion":1,"agentCapabilities":{},"authMethods":[]}
 elif m=="account/read": result={"account":{"type":"chatgpt"},"requiresOpenaiAuth":True}
 elif m=="thread/start":
  assert params["sandbox"]=="read-only"
  assert params["approvalPolicy"]=="never"
  assert params["config"]["mcp_servers"]["theo"]["tools"]["remember"]["approval_mode"]=="approve"
  assert "Theo MCP tools" in params["developerInstructions"]
  assert "Host persona fixture" in params["developerInstructions"]
  assert params["config"]["web_search"]=="disabled"
  for feature in ("multi_agent","multi_agent_v2","goals","memories","in_app_local_automation","shell_tool","unified_exec","apps","plugins","hooks","browser_use","computer_use","image_generation"):
   assert params["config"]["features"][feature] is False
  result={"thread":{"id":"native-fixture"}}
 elif m=="turn/start":
  send({"method":"item/agentMessage/delta","params":{"delta":"Working on this."}})
  send({"method":"item/completed","params":{"item":{"type":"agentMessage","id":"progress","phase":"commentary","text":"Working on this."}}})
  send({"method":"item/agentMessage/delta","params":{"delta":"Fixture answer"}})
  send({"method":"item/completed","params":{"item":{"type":"agentMessage","id":"answer","phase":"final_answer","text":"Fixture answer"}}})
  result={"turn":{"id":"turn-fixture"}}
 elif m=="session/new": result={"sessionId":"native-fixture","configOptions":[{"id":"model","name":"Model","category":"model","type":"select","currentValue":"fixture-model","options":[{"value":"fixture-model","name":"Fixture"}]}]}
 elif m=="session/set_config_option": result={"configOptions":[]}
 elif m=="session/prompt":
  send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"native-fixture","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Fixture answer"}}}})
  result={"stopReason":"end_turn"}
 if "id" in p: send({"jsonrpc":"2.0","id":p["id"],"result":result})
 if m=="turn/start": send({"method":"turn/completed","params":{"turn":{"id":"turn-fixture","status":"completed"}}})
"""


@pytest.mark.parametrize("name", ["claude", "codex", "cursor", "grok"])
async def test_four_real_transports_against_subprocess_protocol_fixture(
    db, settings, tmp_path, monkeypatch, name
):
    path = tmp_path / "native-fixture"
    path.write_text("#!" + sys.executable + "\n" + PROTOCOL_SERVER)
    path.chmod(0o700)
    if name == "claude":
        backend = ClaudeBackend(db, settings, str(path))
    elif name == "codex":
        backend = CodexBackend(db, settings, str(path))
    else:
        backend = ACPBackend(name, db, settings, str(path))

    async def prep(request):
        return {"PATH": os.environ["PATH"]}, {"pool_id": "fixture-only"}

    monkeypatch.setattr(backend, "preparation", prep)
    monkeypatch.setattr(
        "theo.execution.isolation.launch_options",
        lambda settings, root, workspace, command: (command, {}),
    )
    request = ExecutionRequest(
        run_id="run",
        job_id="job",
        conversation_id="conversation",
        owner_id="owner",
        backend=name,
        model="fixture-model",
        lane="interactive",
        context="fixture input",
        instructions="Host persona fixture: be concise.",
        workspace=tmp_path,
        deadline=db.clock() + 60,
        generation=1,
        tool_socket="/fixture",
        tool_token="not-a-live-grant",
    )
    events = [event async for event in backend.events(request)]
    terminal = [event for event in events if event.kind == "terminal"]
    assert len(terminal) == 1
    assert terminal[0].payload["status"] == "completed", terminal[0].payload
    assert terminal[0].payload["text"] == "Fixture answer"
    assert terminal[0].payload["input_tokens"] is None
    if name == "claude":
        assert [event.payload for event in events if event.kind == "runtime_metadata"] == [
            {"model": "fixture-model"}
        ]
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
