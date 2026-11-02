"""Guards against a bug that shipped silently more than once: an
onclick="..." attribute in a template does nothing in the browser.

The panel's CSP is script-src 'self' with no unsafe-inline. That directive
governs inline event handler attributes (onclick, onchange, ...) exactly the
same way it governs inline <script> blocks -- not just the ones with actual
<script> tags. A button relying on onclick="return confirm(...)" to ask
before a destructive action submits immediately instead, with the dialog
silently never appearing; a button relying on onclick to do anything at all
(no other submit trigger) does nothing when clicked. This was found live in
three separate templates (ftp/list.html, stack/index.html, files/browse.html)
during the file manager rework -- this test is what stands between it
coming back a fourth time.
"""

from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "panel" / "app" / "templates"


def test_no_template_uses_an_inline_event_handler_attribute():
    offenders = []
    for template in TEMPLATES_DIR.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "onclick=" in line or "onchange=" in line or "onsubmit=" in line:
                offenders.append(f"{template.relative_to(TEMPLATES_DIR)}:{lineno}")

    assert not offenders, (
        "inline event handler attributes are silently inert under this app's CSP "
        "(script-src 'self', no unsafe-inline) -- use a data-* attribute picked up "
        "by a static/*.js file instead. Found in: " + ", ".join(offenders)
    )


def test_no_template_has_an_inline_script_block():
    """The same CSP also refuses ``<script>...</script>`` blocks. One on the
    Backups page left the "Automated Schedule" tab dead, and one on the SSH
    page left "view public key" dead -- both looked fine in review."""
    import re

    offenders = []
    for template in TEMPLATES_DIR.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        for match in re.finditer(r"<script(?P<attrs>[^>]*)>", text, flags=re.IGNORECASE):
            attrs = match.group("attrs")
            # A JSON data block is never executed, so the CSP has nothing to refuse.
            if "src=" not in attrs and "application/json" not in attrs:
                lineno = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{template.relative_to(TEMPLATES_DIR)}:{lineno}")

    assert not offenders, (
        "inline <script> blocks are blocked by this app's CSP (script-src 'self') -- "
        "move the code to a static/*.js file. Found in: " + ", ".join(offenders)
    )
