"""Build a shareable source archive; exclude profiles, keys, HAR and captures."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    root = Path(__file__).resolve().parent
    output = root.parent / "notion-agent-connector-share.zip"
    names = [
        "README.md", "requirements.txt", "run.ps1", ".gitignore", "index.html",
        "notion_client.py", "session_manager.py", "session_pool.py", "connector_api.py",
        "server.py", "mcp_stdio.py", "package_connector.py",
        "examples/opencode.json", "examples/opencode-v2.json", "examples/mcp-stdio.json",
        "tests/test_connector.py", "tests/test_pool_api.py",
    ]
    for name in names:
        if not (root / name).is_file():
            raise FileNotFoundError(name)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(root / name, "notion-agent-connector/" + name)
    print(f"Archive: {output} ({len(names)} files; no saved sessions)")


if __name__ == "__main__":
    main()
