from src.agents.nodes import parse_design_sections
from src.agents.prompts import DESIGNER_SYSTEM_PROMPT


def test_prompt_requires_frontend_and_backend_standards():
    prompt = DESIGNER_SYSTEM_PROMPT
    # Frontend experience
    for phrase in ("design system", "WCAG 2.2 AA", "Core Web Vitals", "mobile-first", "microcopy", "TypeScript"):
        assert phrase in prompt, phrase
    # Backend build standards
    for phrase in ("versioned migrations", "OWASP ASVS", "12-factor", "contract tests", "OpenAPI", "trust boundary"):
        assert phrase in prompt, phrase


def test_frontend_section_is_conditional():
    assert "include only when the product has a user interface" in DESIGNER_SYSTEM_PROMPT


def test_output_contract_is_unchanged_for_the_parser():
    assert "===ARCHITECTURE===" in DESIGNER_SYSTEM_PROMPT
    assert "===API SPEC===" in DESIGNER_SYSTEM_PROMPT
    assert "(sections 1, 2, 5, 6, 7 and 8 here)" in DESIGNER_SYSTEM_PROMPT
    architecture, api_spec = parse_design_sections(
        "===ARCHITECTURE===\n## 7. Frontend experience\n## 8. Build standards\n===API SPEC===\n## 3. API design"
    )
    assert "Frontend experience" in architecture and "Build standards" in architecture
    assert api_spec.startswith("## 3. API design")
