"""htmx `changed` compares the triggering element's `.value`: a <form> has none, so a form-level
`hx-trigger="input changed"` never fires (the plan sliders and the simulation silently did nothing)."""
import re
from pathlib import Path

TEMPLATES = Path(__file__).parent.parent / 'app' / 'templates'


def test_no_form_uses_changed_modifier():
    offenders = []
    for path in TEMPLATES.rglob('*.html'):
        for tag in re.findall(r'<form\b[^>]*>', path.read_text(encoding='utf-8'), re.S):
            trigger = re.search(r'hx-trigger="([^"]*)"', tag)
            if trigger and re.search(r'\bchanged\b', trigger.group(1)):
                offenders.append(f'{path.relative_to(TEMPLATES)}: {trigger.group(1)}')
    assert offenders == []
