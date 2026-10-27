"""[tamako-local] ``export_file`` — host-pull a container file out to the host inbox.

Same family as ``relay_to_owner`` (shares the ``relay`` toolset and
``tools/media_delivery.py``). Purpose: a sandboxed chat (group / non-owner DM)
can hand over a file it produced INSIDE the container, and then deliver it with
the normal ``MEDIA:`` mechanism.

Direction matters: the SANDBOX never writes the host. The host reads a bounded
slice of a container path (under a fixed root) and writes its own copy into the
host inbox — so the caller can only ever name a container path, never a host
path. The tool returns that host path plus a ready ``MEDIA:`` line.

One hard constraint: the sandbox is one container per turn, discarded afterwards
(``container_persistent: false``), so the export MUST happen in the same turn as
the delivery — a "say something now, fetch next turn" cannot work.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from tools.media_delivery import DeliveryError, DeliveryMedia, local_backend
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_TOOL_NAME = "export_file"
_TOOLSET = "relay"


EXPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": (
            "把沙箱容器里的一个文件取出来交给宿主（宿主主动取，落到宿主媒体收件箱），"
            "返回宿主路径和一条现成的 MEDIA: 行，随后用它正常交付。"
            "只允许容器内固定目录（默认 /root/export/）下的文件；有类型/大小/每轮次数上限。"
            "必须在同一轮内使用（沙箱每轮一个新容器，本轮结束就取不到了）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "容器内的文件路径（默认仅 /root/export/ 底下的文件可导出）",
                }
            },
            "required": ["path"],
        },
    },
}


def export_file(args, task_id=None, **kw) -> str:
    """Tool handler: host-pull ``args['path']`` from the sandbox into the host inbox."""
    raw = str(args.get("path") or "").strip()
    if not raw:
        return tool_error("导出失败：没有给文件路径。")

    policy = DeliveryMedia.from_config()

    # Local backend: the file is already on the host — just hand the path back.
    if local_backend():
        try:
            p = Path(raw).expanduser()
        except Exception:
            return tool_error(f"导出失败：路径无法解析：{raw}")
        if not p.is_file():
            return tool_error(f"导出失败：本地没有这个文件：{raw}")
        return json.dumps(
            {"success": True, "host_path": str(p),
             "media_line": DeliveryMedia.render_media_line(p),
             "note": "本地后端，文件本就在宿主上，无需导出。"},
            ensure_ascii=False,
        )

    try:
        host_path = policy.pull(task_id, raw)
    except DeliveryError as exc:
        return tool_error(f"导出失败：{exc}")
    except Exception as exc:  # pragma: no cover - unexpected env failure
        logger.exception("export_file pull failed")
        return tool_error(f"导出失败：{exc}")

    return json.dumps(
        {
            "success": True,
            "host_path": str(host_path),
            "media_line": DeliveryMedia.render_media_line(host_path),
            "bytes": host_path.stat().st_size,
            "note": "把 media_line 放进本轮回复即可交付；仅本轮有效（沙箱随之销毁）。",
        },
        ensure_ascii=False,
    )


registry.register(
    name=_TOOL_NAME,
    toolset=_TOOLSET,
    schema=EXPORT_SCHEMA,
    handler=export_file,
    emoji="📤",
    max_result_size_chars=2000,
)
