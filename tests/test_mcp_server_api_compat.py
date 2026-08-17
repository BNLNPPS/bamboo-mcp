"""Guard that the installed mcp exposes the low-level Server API core.py uses.

``core.build_server()`` registers handlers with the low-level decorator API
(``@app.list_tools()``, ``@app.call_tool()``, ``@app.list_prompts()``,
``@app.get_prompt()``).  mcp 2.0.0 removed those decorators.

The failure mode this guards against is unusually quiet.  Those decorators run
inside ``build_server()``, which the test suite never calls, so the whole suite
passes green against an mcp version that cannot start the server at all.  The
incompatibility first showed up only as four pyright ``reportAttributeAccessIssue``
errors in CI — and only in CI, because the requirement was ``mcp>=0.9.0`` with no
upper bound, so a fresh CI install resolved 2.0.0 while developer machines kept
an older 1.x.

``requirements.txt`` now pins ``mcp>=0.9.0,<2.0.0``. This test makes the
constraint self-enforcing: if the pin is lifted without porting ``core.py``, a
test fails rather than the server failing at start-up in production.
"""
from __future__ import annotations

import pytest

# The decorator factories build_server() calls on the Server instance.
_REQUIRED_SERVER_DECORATORS: tuple[str, ...] = (
    "list_tools",
    "call_tool",
    "list_prompts",
    "get_prompt",
)


@pytest.mark.parametrize("attribute", _REQUIRED_SERVER_DECORATORS)
def test_server_exposes_decorator_used_by_build_server(attribute: str) -> None:
    """The installed mcp Server must expose each decorator core.py registers with.

    Args:
        attribute: Name of the decorator factory expected on ``Server``.
    """
    mcp_server = pytest.importorskip(
        "mcp.server",
        reason="mcp is not installed; nothing to verify.",
    )

    server_cls = getattr(mcp_server, "Server", None)
    assert server_cls is not None, (
        "mcp.server.Server is missing entirely — core.build_server() cannot work "
        "with this mcp version."
    )

    assert hasattr(server_cls, attribute), (
        f"mcp.server.Server has no {attribute!r}. core.build_server() uses "
        f"@app.{attribute}() to register handlers, so the MCP server will fail "
        f"at start-up with this mcp version even though the test suite passes "
        f"(the decorators run inside build_server(), which tests do not call). "
        f"mcp 2.0.0 removed this API; requirements.txt pins mcp<2.0.0 for that "
        f"reason. If the pin has been lifted deliberately, port core.py to the "
        f"new API before removing this test."
    )
