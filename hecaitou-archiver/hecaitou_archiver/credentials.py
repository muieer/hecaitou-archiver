"""Read the Ark credential from a local text file; never include its value in errors."""
from pathlib import Path

DEFAULT_KEY_FILE = Path(__file__).resolve().parents[1] / "secrets" / "ark_api_key.txt"


def read_api_key(path=DEFAULT_KEY_FILE):
    try:
        value = Path(path).expanduser().read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError("无法读取 Ark API Key txt 文件，请检查文件路径、权限及 UTF-8 编码") from exc
    if not value or not value.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("Ark API Key txt 必须包含一个非空、无内部空白的 Key")
    return value
