"""show/server.py —— 本地只读工作台：把已经渲染好的答案页挂在 127.0.0.1 上给人看。

边界（contract/answer.md 第 8.4 节 + 第 1 节「不定义 UI」）：
  * **只绑 127.0.0.1**，不提供 `--host` 换绑，也不监听公网；端口可换。
  * **只读**：只认 GET / HEAD，其余方法（POST / PUT / DELETE / PATCH / OPTIONS …）
    一律 405，且 `Allow: GET, HEAD`。P0 没有写操作，一条都不开口子。
  * 页面内容只来自两处：`--root` 目录里已经生成好的静态文件（默认 `show/out/`），
    以及 `--answers-dir` 里**显式指定**的 `answer.json`（在内存里用 show/render.py
    渲染，绝不落盘、绝不改写它）。不给 `--answers-dir` 就只当静态文件服务器用。
  * 不 import `ask/`、不读源表格、不连数据库、不重算任何数字 —— 这一层只是搬运。
  * 路径穿越（`..`）、绝对路径、越界符号链接一律拒；不列 `--root` 之外的目录。

用法：
  python -m show.server                       # http://127.0.0.1:8765/ ，服务 show/out/
  python -m show.server --port 8802 --root show/out
  python -m show.server --answers-dir <answer.json 所在目录>   # 再加一条 /render/<名>.json
"""

from __future__ import annotations

import argparse
import html
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from show import render as render_mod

# 契约 8.4：工作台只绑 127.0.0.1。这里写死，不留换绑的口子。
BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_ROOT = "show/out"

ALLOWED_METHODS = "GET, HEAD"
# 只往外发这几类文件：别的一律 404，免得把目录里的杂物顺带暴露出去
SERVED_SUFFIXES = (".html", ".css", ".svg", ".json", ".txt", ".md", ".png")
RENDER_PREFIX = "/render/"

READ_ONLY_NOTE = "只读工作台 · 仅绑定 127.0.0.1 · 非 GET/HEAD 一律 405（契约 8.4）"


class WorkbenchError(Exception):
    """请求无法满足（路径越界 / 文件不存在 / 渲染不了）。"""

    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def esc(text: object) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def page(title: str, body: str) -> str:
    """工作台自己的小页面（列目录 / 报错）。内容全是本地生成，不含脚本。"""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)}</title>\n<style>\n"
        "body{margin:0;padding:28px 20px 56px;background:#f6f8fb;color:#0f172a;"
        'font-family:system-ui,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.6}\n'
        "main{max-width:920px;margin:0 auto}\n"
        "h1{font-size:20px;margin:0 0 6px}\n"
        ".lede{color:#64748b;font-size:13px;margin:0 0 18px}\n"
        "ul{list-style:none;margin:0;padding:0;background:#fff;border:1px solid #e2e8f0;"
        "border-radius:10px;overflow:hidden}\n"
        "li{border-bottom:1px solid #f1f5f9}\n"
        "li:last-child{border-bottom:none}\n"
        "a{display:flex;justify-content:space-between;gap:12px;padding:10px 16px;"
        "color:#2f6fed;text-decoration:none;font-family:ui-monospace,Consolas,monospace;font-size:13px}\n"
        "a:hover{background:#f8fafc}\n"
        "a span{color:#94a3b8;font-family:system-ui,sans-serif}\n"
        "code{font-family:ui-monospace,Consolas,monospace;"
        "background:#eef2ff;border-radius:4px;padding:1px 5px}\n"
        "footer{color:#94a3b8;font-size:12px;text-align:center;margin-top:22px}\n"
        "</style>\n</head>\n<body>\n<main>\n"
        f"{body}</main>\n</body>\n</html>\n"
    )


def listing_page(root: Path) -> str:
    entries = []
    if root.is_dir():
        for item in sorted(root.iterdir(), key=lambda p: p.name):
            if not item.is_file() or item.suffix.lower() not in SERVED_SUFFIXES:
                continue
            entries.append(
                f'<li><a href="/{esc(item.name)}">{esc(item.name)}'
                f'<span>{esc(human_size(item.stat().st_size))}</span></a></li>'
            )
    if not entries:
        body_list = '<p class="lede">目录里还没有生成的页面。</p>'
    else:
        body_list = f"<ul>{''.join(entries)}</ul>"
    return page(
        "tally 工作台",
        "<h1>tally 工作台</h1>\n"
        f'<p class="lede">{esc(READ_ONLY_NOTE)}<br>'
        f"服务目录 <code>{esc(root.as_posix())}</code></p>\n"
        f"{body_list}\n"
        '<footer>show/server.py · 纯标准库 · 无脚本、不联网、不写盘</footer>',
    )


def resolve_inside(base: Path, relative: str) -> Path:
    """把 URL 里的相对路径钉死在 `base` 之内；`..` / 绝对路径 / 越界一律拒。"""
    raw = unquote(relative).replace("\\", "/").strip("/")
    if not raw:
        raise WorkbenchError(HTTPStatus.NOT_FOUND, "空路径")
    if ".." in raw.split("/"):
        raise WorkbenchError(HTTPStatus.BAD_REQUEST, "路径里不许出现 ..（目录穿越）")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise WorkbenchError(HTTPStatus.BAD_REQUEST, "只接受相对路径")
    base_resolved = base.resolve()
    target = (base_resolved / candidate).resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise WorkbenchError(HTTPStatus.BAD_REQUEST, "路径跑到服务目录外面了")
    return target


def render_answer_html(answer_path: Path) -> str:
    """用 show/render.py 在内存里渲染一份 answer.json；只读、不落盘。"""
    try:
        answer = render_mod.load_answer(answer_path)
    except render_mod.ShowError as exc:
        raise WorkbenchError(HTTPStatus.UNPROCESSABLE_ENTITY, f"这份 answer.json 渲染不了：{exc}") from exc
    columns = render_mod.columns_by_name(answer)
    _, by_path = render_mod.trace_maps(answer)
    plans = [
        render_mod.plan_chart(answer, chart, columns, by_path)
        for chart in (answer.get("charts") or [])
        if isinstance(chart, dict)
    ]
    checks = render_mod.run_checks(answer, plans)
    return render_mod.render_page(answer, checks)


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "tally-show/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # 由 main() 注入
    root: Path = Path(DEFAULT_ROOT)
    answers_dir: Path | None = None

    # ---- 响应工具 --------------------------------------------------------

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, *, head_only: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == HTTPStatus.METHOD_NOT_ALLOWED:
            self.send_header("Allow", ALLOWED_METHODS)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, text: str, *, head_only: bool) -> None:
        self._send(status, text.encode("utf-8"), "text/html; charset=utf-8", head_only=head_only)

    def _send_error_page(self, status: HTTPStatus, message: str, *, head_only: bool) -> None:
        body = page(
            f"{status.value} {status.phrase}",
            f"<h1>{status.value} {status.phrase}</h1>\n"
            f'<p class="lede">{esc(message)}</p>\n'
            f'<p class="lede">{esc(READ_ONLY_NOTE)}</p>\n'
            '<footer>show/server.py</footer>',
        )
        self._send_html(status, body, head_only=head_only)

    # ---- 只读闸：非 GET / HEAD 一律 405 -----------------------------------

    def _reject_write(self) -> None:
        # 请求体我们不读，所以这条连接直接关掉，免得下一个请求读到脏字节
        self.close_connection = True
        self._send_error_page(
            HTTPStatus.METHOD_NOT_ALLOWED,
            f"{self.command} 不受支持：这个工作台是只读的，只认 {ALLOWED_METHODS}（契约 8.4）。",
            head_only=False,
        )

    do_POST = _reject_write
    do_PUT = _reject_write
    do_DELETE = _reject_write
    do_PATCH = _reject_write
    do_OPTIONS = _reject_write
    do_TRACE = _reject_write
    do_CONNECT = _reject_write

    # ---- GET / HEAD ------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        self._handle(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(head_only=True)

    def _handle(self, *, head_only: bool) -> None:
        path = urlsplit(self.path).path or "/"
        try:
            if path.startswith(RENDER_PREFIX):
                self._serve_rendered(path[len(RENDER_PREFIX):], head_only=head_only)
            elif path in ("/", "/index.html") and not (self.root / "index.html").is_file():
                self._send_html(HTTPStatus.OK, listing_page(self.root), head_only=head_only)
            elif path in ("/", "/index.html"):
                self._serve_static("index.html", head_only=head_only)
            elif path == "/_list":
                self._send_html(HTTPStatus.OK, listing_page(self.root), head_only=head_only)
            else:
                self._serve_static(path, head_only=head_only)
        except WorkbenchError as exc:
            self._send_error_page(exc.status, exc.message, head_only=head_only)

    def _serve_static(self, relative: str, *, head_only: bool) -> None:
        target = resolve_inside(self.root, relative)
        if target.suffix.lower() not in SERVED_SUFFIXES:
            raise WorkbenchError(
                HTTPStatus.NOT_FOUND, f"不提供 {target.suffix or '无后缀'} 这类文件"
            )
        if not target.is_file():
            raise WorkbenchError(HTTPStatus.NOT_FOUND, f"没有这个文件：{relative}")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/json", "image/svg+xml"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type, head_only=head_only)

    def _serve_rendered(self, relative: str, *, head_only: bool) -> None:
        if self.answers_dir is None:
            raise WorkbenchError(
                HTTPStatus.NOT_FOUND,
                "没有配置 --answers-dir，所以没有可现渲染的 answer.json；"
                "这里只能看已经生成好的页面。",
            )
        target = resolve_inside(self.answers_dir, relative)
        if target.suffix.lower() != ".json":
            raise WorkbenchError(HTTPStatus.NOT_FOUND, "这里只渲染 .json（一份 answer.json 一页）")
        if not target.is_file():
            raise WorkbenchError(HTTPStatus.NOT_FOUND, f"没有这个文件：{relative}")
        self._send_html(HTTPStatus.OK, render_answer_html(target), head_only=head_only)

    # ---- 日志 ------------------------------------------------------------

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        sys.stderr.write(f"show.server: {self.address_string()} {format % args}\n")


def make_server(root: Path, answers_dir: Path | None, port: int) -> ThreadingHTTPServer:
    handler = type(
        "BoundWorkbenchHandler",
        (WorkbenchHandler,),
        {"root": root, "answers_dir": answers_dir},
    )
    server = ThreadingHTTPServer((BIND_HOST, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m show.server",
        description="本地只读工作台：只绑 127.0.0.1，只认 GET/HEAD，只读不写（契约 8.4）。",
    )
    parser.add_argument("--root", default=DEFAULT_ROOT, help="静态页面目录（默认 show/out）")
    parser.add_argument(
        "--answers-dir",
        default=None,
        help="若要现渲染，给一个放 answer.json 的目录；只读、在内存里渲染，不落盘",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 8765）")
    parser.add_argument(
        "--host",
        default=BIND_HOST,
        help="只为把「绑不了别处」这件事说清楚；除 127.0.0.1 之外一律拒绝",
    )
    args = parser.parse_args(argv)

    if args.host != BIND_HOST:
        print(
            f"show.server: 拒绝绑定 {args.host}：契约 8.4 规定工作台只绑 {BIND_HOST}，"
            "没有换绑的口子。",
            file=sys.stderr,
        )
        return 2

    root = Path(args.root)
    if not root.is_dir():
        print(f"show.server: 目录不存在：{root.as_posix()}", file=sys.stderr)
        return 2
    answers_dir = Path(args.answers_dir) if args.answers_dir else None
    if answers_dir is not None and not answers_dir.is_dir():
        print(f"show.server: 目录不存在：{answers_dir.as_posix()}", file=sys.stderr)
        return 2

    try:
        server = make_server(root, answers_dir, args.port)
    except OSError as exc:
        print(f"show.server: 绑不上 {BIND_HOST}:{args.port}：{exc}", file=sys.stderr)
        return 2

    host, port = server.server_address[0], server.server_address[1]
    print(f"show.server: http://{host}:{port}/  ·  {READ_ONLY_NOTE}", file=sys.stderr)
    print(f"show.server: 服务目录 {root.as_posix()}", file=sys.stderr)
    if answers_dir is not None:
        print(f"show.server: 现渲染入口 {RENDER_PREFIX}<名>.json ← {answers_dir.as_posix()}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("show.server: 已停止", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
