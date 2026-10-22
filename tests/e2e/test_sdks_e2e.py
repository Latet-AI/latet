"""The official OpenAI, Anthropic and Google GenAI SDKs, and LangChain and LangGraph on the OpenAI one, through Latet's
gateway over real HTTP, to a fake provider on this computer: what the documentation's "Scripts and SDKs" promises. Each
reads its base URL from the variables `latet run` sets, the provider never reads the secret, the address or the IBAN,
and the script gets them back in the answer. AutoGen and CrewAI, which do not fit this environment, are in
tests/agents/test_frameworks.py."""

import socket
import threading
import time

import pytest
import uvicorn

from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.cloud import VertexUpstream
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
EMAIL = "mario.rossi@acme.it"
IBAN = "IT60 X054 2811 1010 0000 0123 456"
CANARIES = (GH_PAT, EMAIL, IBAN)
PROMPT = f"deploy with key {GH_PAT}, notify {EMAIL}, pay to {IBAN}"
ECHO = "You said: " + PROMPT  # what the fake answers, once Latet has put the originals back


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream(real=True).start()
    yield fake
    fake.stop()


@pytest.fixture(scope="module")
def gateway(upstream):
    """Latet's gateway on a free port, OpenAI and Anthropic pointed at the fake: the base URLs `latet run` sets."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    built = build(Config(), Vault(ROOT_KEY))
    config = GatewayConfig(
        port=port,
        upstreams=[
            Upstream("anthropic", "/anthropic", upstream.url),
            Upstream("openai", "/openai", upstream.url),
            Upstream("gemini", "/gemini", upstream.url, refuses_unread=True),
            VertexUpstream(base=upstream.url),
        ],
        mediator=Mediator(built.engine, built.verifier),
    )
    server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def latet_run(gateway, monkeypatch):
    """The variables `latet run` gives the command it runs (``latet.cli``'s ``route_env``), and a key for the fake."""
    monkeypatch.setenv("OPENAI_BASE_URL", f"{gateway}/openai/v1")
    monkeypatch.setenv("OPENAI_API_BASE", f"{gateway}/openai/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"{gateway}/anthropic")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", f"{gateway}/gemini")
    monkeypatch.setenv("GOOGLE_VERTEX_BASE_URL", f"{gateway}/vertex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    for name in ("GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    return gateway


def _never_sent(upstream) -> None:
    sent = upstream.all_received()
    for canary in CANARIES:
        assert canary not in sent


def test_the_openai_sdk_chat_gets_its_answer_back_and_the_provider_reads_no_secret(latet_run, upstream):
    from openai import OpenAI

    client = OpenAI()
    reply = client.chat.completions.create(model="your-model", messages=[{"role": "user", "content": PROMPT}])
    assert reply.choices[0].message.content == ECHO
    streamed = client.chat.completions.create(
        model="your-model", messages=[{"role": "user", "content": PROMPT}], stream=True
    )
    assert "".join(c.choices[0].delta.content or "" for c in streamed if c.choices) == ECHO
    _never_sent(upstream)


def test_the_openai_sdk_responses_get_their_answer_back_and_the_provider_reads_no_secret(latet_run, upstream):
    from openai import OpenAI

    events = OpenAI().responses.create(model="your-model", input=PROMPT, stream=True)
    text = "".join(e.delta for e in events if e.type == "response.output_text.delta")
    assert text == ECHO
    _never_sent(upstream)


def test_the_anthropic_sdk_gets_its_answer_back_and_the_provider_reads_no_secret(latet_run, upstream):
    from anthropic import Anthropic

    client = Anthropic()
    message = client.messages.create(model="claude-x", max_tokens=200, messages=[{"role": "user", "content": PROMPT}])
    assert message.content[0].type == "text" and message.content[0].text == ECHO
    with client.messages.stream(
        model="claude-x", max_tokens=200, messages=[{"role": "user", "content": PROMPT}]
    ) as stream:
        assert "".join(stream.text_stream) == ECHO
    _never_sent(upstream)


def test_langchain_on_the_openai_sdk_gets_its_answer_back_and_the_provider_reads_no_secret(latet_run, upstream):
    from langchain_openai import ChatOpenAI

    reply = ChatOpenAI(model="your-model").invoke(PROMPT)
    assert reply.content == ECHO
    _never_sent(upstream)


def test_a_langgraph_graph_on_langchain_gets_its_answer_back_and_the_provider_reads_no_secret(latet_run, upstream):
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI
    from langgraph.graph import END, START, MessagesState, StateGraph

    model = ChatOpenAI(model="your-model")

    def call(state: MessagesState) -> MessagesState:
        return {"messages": [model.invoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", call)
    graph.add_edge(START, "model")
    graph.add_edge("model", END)
    asked: MessagesState = {"messages": [HumanMessage(PROMPT)]}
    out = graph.compile().invoke(asked)
    assert out["messages"][-1].content == ECHO
    _never_sent(upstream)


def test_the_genai_sdk_gets_its_answer_back_and_the_provider_reads_no_secret(latet_run, upstream):
    from google import genai

    client = genai.Client()
    reply = client.models.generate_content(model="gemini-3-pro", contents=PROMPT)
    assert reply.text == ECHO
    streamed = client.models.generate_content_stream(model="gemini-3-pro", contents=PROMPT)
    assert "".join(chunk.text or "" for chunk in streamed) == ECHO
    assert upstream.requests[-1]["headers"]["x-goog-api-key"] == "gemini-test-key"
    _never_sent(upstream)


def test_the_genai_sdk_on_vertex_with_an_api_key_gets_its_answer_back(latet_run, upstream, monkeypatch):
    from google import genai

    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_API_KEY", "vertex-test-key")
    monkeypatch.delenv("GEMINI_API_KEY")
    client = genai.Client()  # kept: a client no longer referenced closes its connections
    reply = client.models.generate_content(model="gemini-3-pro", contents=PROMPT)
    assert reply.text == ECHO
    assert "/publishers/google/models/gemini-3-pro:generateContent" in upstream.requests[-1]["path"]
    _never_sent(upstream)


def test_the_genai_sdk_on_vertex_with_a_project_gets_its_answer_back(latet_run, upstream):
    from google import genai
    from google.oauth2.credentials import Credentials

    client = genai.Client(vertexai=True, project="p", location="us-central1", credentials=Credentials("ya29.fake"))
    streamed = client.models.generate_content_stream(model="gemini-3-pro", contents=PROMPT)
    assert "".join(chunk.text or "" for chunk in streamed) == ECHO
    assert upstream.requests[-1]["headers"]["authorization"] == "Bearer ya29.fake"
    _never_sent(upstream)
