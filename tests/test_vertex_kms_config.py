"""
vertexai.init() writes process-global state, so one KMS model's configuration can
be replaced by another request's while a model built from it is still in use.

A GenerativeModel reads that global state twice: at construction, and again on
first use, when its prediction client is built and captures credentials, endpoint
and project. Only the first was covered by the lock, so a model could end up
talking to the project and CMEK key of whichever request happened to re-init in
between — for the whole life of that cached model.
"""
import pytest

import vertexai
from unillm.llm import vertex_ai_kms
from unillm.llm.vertex_ai_kms import VertexAIKMSHandler


@pytest.fixture
def sdk(monkeypatch):
    """Stand-in for the Vertex SDK that records what the global config was when read."""
    state = {"config": None, "inits": 0}

    def fake_init(project=None, location=None, encryption_spec_key_name=None):
        state["config"] = (project, location, encryption_spec_key_name)
        state["inits"] += 1

    class FakeModel:
        def __init__(self, model_name, system_instruction=None):
            self.model_name = model_name
            self.system_instruction = system_instruction
            self.built_under = state["config"]
            self._client = None

        @property
        def _prediction_client(self):
            # Mirrors the SDK's cached_property: resolved once, on first touch.
            if self._client is None:
                self._client = state["config"]
            return self._client

    monkeypatch.setattr(vertexai, "init", fake_init)
    monkeypatch.setattr(vertex_ai_kms, "GenerativeModel", FakeModel)
    monkeypatch.setattr(vertex_ai_kms, "_global_vertexai_config", None)
    return state


A = ("proj-a", "us-central1", "key-a")
B = ("proj-b", "europe-west4", "key-b")


def test_a_model_keeps_the_config_it_was_built_for(sdk):
    handler = VertexAIKMSHandler()
    model_a = handler._get_model("gemini-2.5-flash", *A)

    # Another request switches the process-global SDK config underneath it.
    handler._get_model("gemini-2.5-flash", *B)

    assert model_a.built_under == A
    # The client too — this is the one that used to be resolved lazily, and so
    # picked up whatever config happened to be installed at first use.
    assert model_a._prediction_client == A


def test_models_for_different_configs_do_not_share_a_cache_entry(sdk):
    handler = VertexAIKMSHandler()
    model_a = handler._get_model("gemini-2.5-flash", *A)
    model_b = handler._get_model("gemini-2.5-flash", *B)

    assert model_a is not model_b
    assert model_b._prediction_client == B
    # And going back to the first config reuses its entry rather than rebuilding
    # against whatever the SDK currently holds.
    assert handler._get_model("gemini-2.5-flash", *A) is model_a


def test_a_repeated_config_does_not_re_initialize_the_sdk(sdk):
    handler = VertexAIKMSHandler()
    handler._get_model("gemini-2.5-flash", *A)
    handler._get_model("gemini-2.5-flash", *A)
    handler._get_model("gemini-2.5-flash", *A, system_instruction="be brief")
    assert sdk["inits"] == 1


def test_system_instruction_still_separates_entries(sdk):
    handler = VertexAIKMSHandler()
    plain = handler._get_model("gemini-2.5-flash", *A)
    prompted = handler._get_model("gemini-2.5-flash", *A, system_instruction="be brief")
    assert plain is not prompted
    assert prompted.system_instruction == "be brief"


def test_the_cache_stays_bounded(sdk):
    handler = VertexAIKMSHandler()
    handler._models_cache_max = 4
    for i in range(9):
        handler._get_model("gemini-2.5-flash", *A, system_instruction=f"prompt-{i}")
    assert len(handler._models) <= 4
