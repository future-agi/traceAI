"""A real MCP server over stdio whose tools return synthetic typed media.

Used by the MCP (``wrap_mcp_client``) and mcp-use tests. It talks to nothing
else. ``reads`` reports how many times the client read each resource, so a
test can prove a ``resource_link`` URI was never fetched.
"""

import base64
import json
import struct
import zlib
from importlib.metadata import version

from mcp import types

if int(version("mcp").split(".")[0]) >= 2:
    from mcp.server.mcpserver import MCPServer as Server
else:
    from mcp.server.fastmcp import FastMCP as Server


def png(seed=0, width=2, height=2) -> bytes:
    raw = b"".join(b"\x00" + bytes((seed + x + y) % 256 for x in range(width * 3)) for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


WAV = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00@\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
SECRET_FILE = "file:///etc/hostname"
READS = {"shot": 0, "secret": 0}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def block(kind, **fields):
    """A content block from its wire (camelCase) shape, valid on MCP 1.x and 2.x."""
    model = {"image": types.ImageContent, "audio": types.AudioContent, "text": types.TextContent,
             "resource": types.EmbeddedResource, "resource_link": types.ResourceLink}[kind]
    return model.model_validate({"type": kind, **fields})


server = Server("media-fixture")


@server.tool()
def screenshot():
    """A screenshot and a caption."""
    return [block("text", text="CAPTION-MARKER"), block("image", data=b64(png(1)), mimeType="image/png")]


@server.tool()
def voice():
    """A short WAV clip."""
    return [block("audio", data=b64(WAV), mimeType="audio/wav")]


@server.tool()
def report():
    """An embedded PDF resource blob plus a resource link that must never be fetched."""
    return [
        block("resource", resource={"uri": "file:///report.pdf", "mimeType": "application/pdf", "blob": b64(PDF)}),
        block("resource_link", uri=SECRET_FILE, name="hostname", mimeType="image/png"),
    ]


@server.tool()
def relabeled():
    """An image block whose MIME claims another modality, and one outside the F01 enum."""
    return [block("image", data=b64(png(2)), mimeType="audio/wav"),
            block("image", data=b64(png(3)), mimeType="image/gif")]


@server.tool()
def malformed():
    """Not valid base64."""
    return [block("image", data="!!!not-base64!!!", mimeType="image/png")]


@server.tool()
def many(count: int):
    """``count`` distinct images."""
    return [block("image", data=b64(png(10 + n)), mimeType="image/png") for n in range(count)]


@server.tool()
def big():
    """An image larger than the 256 KiB per-result budget."""
    return [block("image", data=b64(png(4) + b"\x00" * (300 * 1024)), mimeType="image/png")]


@server.tool()
def reads() -> str:
    return json.dumps(READS)


@server.resource("file:///shot.png", mime_type="image/png")
def shot() -> bytes:
    READS["shot"] += 1
    return png(5)


@server.resource(SECRET_FILE, mime_type="image/png")
def secret() -> bytes:
    READS["secret"] += 1
    return png(6)


if __name__ == "__main__":
    server.run(transport="stdio")
