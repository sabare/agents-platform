from agents_platform.config import Settings
from agents_platform.demo_policy import model_instructions


def test_demo_policy_is_explicit_and_keeps_evidence_and_permission_boundaries():
    strict = Settings(_env_file=None)
    assert model_instructions("Original instructions", strict) == "Original instructions"
    demo = Settings(_env_file=None, demo_assume_routine_updates=True)
    prompt = model_instructions("Original instructions", demo)
    assert prompt.startswith("Original instructions")
    assert "Choose a reasonable default" in prompt
    assert "three days" in prompt
    assert "do not claim a file arrived without saved document evidence" in prompt
    assert "Authorization, consent, explicit" in prompt
