"""Python runtime tooling for Python sites.

``python3`` itself ships with every Debian/Ubuntu install and a good deal of
the OS depends on it, so this provider never touches the interpreter.  What
sites actually need on top of it is ``venv`` (each site gets its own
virtualenv, so two apps never fight over package versions) and ``pip``, plus
the headers most wheels need when they have to compile.
"""

from __future__ import annotations

from typing import List, Optional

from app.providers.base import Provider, register
from app.services import apt

# python3 is deliberately absent from the removal set -- see the module doc.
PACKAGES = ["python3", "python3-venv", "python3-pip", "python3-dev", "build-essential"]
REMOVABLE = ["python3-venv", "python3-pip", "python3-dev"]
PROBE_PACKAGE = "python3-venv"


@register
class PythonProvider(Provider):
    key = "python"
    name = "Python"
    description = "Python 3 with venv and pip, for running Python sites (Flask, Django, FastAPI)."
    category = "tools"

    def installed_versions(self) -> List[str]:
        # python3-venv is what separates "has an interpreter" (always true)
        # from "can host a Python site".
        version = apt.installed_version(PROBE_PACKAGE)
        return [version] if version else []

    def install(self, ctx, version: Optional[str] = None) -> None:
        if self.is_installed():
            ctx.log("Python tooling is already installed")
            return
        ctx.log("Installing Python 3 with venv and pip")
        apt.install(ctx, PACKAGES)
        ctx.log("Python tooling installed")

    def uninstall(self, ctx, version: Optional[str] = None) -> None:
        ctx.log("Removing Python venv/pip tooling (the python3 interpreter is kept)")
        apt.remove(ctx, REMOVABLE)
        ctx.log("Python tooling removed")
