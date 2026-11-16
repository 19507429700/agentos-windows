"""写飞书文档：Agent 写一份「建文档 / 往文档里补内容」的请求，桥接服务以你本人身份去建、去补。

Agent 拿不到任何飞书凭据，只能在 spaces/me/outbox/feishu-docs/<任务号>/ 写 JSON 请求：
  新建：{"title": "标题", "markdown": "spaces/me/outbox/草稿/12/正文.md"}
  补充：{"append_to": "文档链接或标题", "markdown": "spaces/me/outbox/草稿/12/补充.md"}
任务做完后桥接服务逐个处理，把文档链接发回飞书。

- 用你本人的授权（~/.agentos/feishu_user_token.json），文档建在你的云盘里（FEISHU_DOC_FOLDER 指定文件夹，
  不填就在「我的空间」根目录），默认只有你自己能看。
- 只往 AgentOS 自己建的文档里补内容（记录在 ~/.agentos/feishu_docs_written.json），
  不改你或别人原有的文档；只追加到末尾，不删不改已有内容。
- Markdown 里的本地图片（spaces/me/ 下的文件）会上传进文档；网上的图片换成链接。
"""

from __future__ import annotations

import json
import mimetypes
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import feishu_user as fu

DOC_ID_RE = re.compile(r"(?:/docx/)?([A-Za-z0-9]{22,32})/?(?:[?#].*)?$")
IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
PLACEHOLDER = "https://agentos.invalid/img/{}.png"
MAX_MD_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024        # 飞书文档图片上限 20MB
MAX_IMAGES = 50
MAX_BLOCKS_PER_CALL = 500                 # 一次插入的块数，留足余量（接口上限 1000）
TITLE_MAX = 800
CALL_GAP_SEC = 0.35                       # 文档接口每秒约 3 次，图片逐张传时稍微隔开
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class DocError(ValueError):
    pass


# ---------- 请求 ----------

def load_request(path: Path, runtime: Path) -> dict:
    """读并校验请求；不合格抛 DocError（原因会推给你）。"""
    try:
        d = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise DocError(f"请求不是合法 JSON：{e}") from None
    if not isinstance(d, dict):
        raise DocError("请求应该是一个 JSON 对象")
    title = str(d.get("title") or "").strip()
    target = str(d.get("append_to") or "").strip()
    if not title and not target:
        raise DocError("要写 title（新建）或 append_to（往已有文档里补）")
    if len(title) > TITLE_MAX:
        raise DocError(f"标题超过 {TITLE_MAX} 个字")
    md_path = _inside(d.get("markdown"), runtime, [runtime / "spaces/me/outbox"], "正文 markdown")
    if md_path.stat().st_size > MAX_MD_BYTES:
        raise DocError("正文超过 1MB，拆成几次补充")
    md = md_path.read_text("utf-8", errors="replace")
    if not md.strip():
        raise DocError("正文是空的")
    return {"title": title, "append_to": target, "markdown": md, "md_path": md_path}


def _inside(p, runtime: Path, bases: list[Path], what: str) -> Path:
    if not p:
        raise DocError(f"没有写{what}的路径")
    q = Path(str(p))
    q = (q if q.is_absolute() else runtime / q).resolve()
    if not any(q.is_relative_to(b.resolve()) for b in bases):
        raise DocError(f"{what}只能放在 {' 或 '.join(str(b.relative_to(runtime)) for b in bases)} 下：{p}")
    if not q.is_file():
        raise DocError(f"{what}不存在：{p}")
    return q


def prepare_markdown(md: str, runtime: Path, md_dir: Path) -> tuple[str, dict[str, Path], list[str]]:
    """本地图片换成占位网址（转换后按占位找回图片块再上传）；网上的图片换成链接。"""
    images: dict[str, Path] = {}
    problems: list[str] = []

    def sub(m):
        alt, src = m.group(1), m.group(2)
        if re.match(r"https?://", src):
            return f"[图：{alt or src}]({src})"
        p = Path(src)
        cands = [p] if p.is_absolute() else [runtime / p, md_dir / p]
        for c in cands:
            c = c.resolve()
            if c.is_relative_to((runtime / "spaces/me").resolve()) and c.is_file():
                if c.suffix.lower() not in IMAGE_EXTS:
                    break
                if c.stat().st_size > MAX_IMAGE_BYTES:
                    problems.append(f"{c.name} 超过 20MB")
                    return f"（图：{alt or c.name}，太大没放进来）"
                if len(images) >= MAX_IMAGES:
                    problems.append(f"图片超过 {MAX_IMAGES} 张，后面的没放")
                    return f"（图：{alt or c.name}）"
                url = PLACEHOLDER.format(len(images))
                images[url] = c
                return f"![{alt}]({url})"
        problems.append(f"找不到图片 {src}")
        return f"（图：{alt or src}，找不到文件）"

    return IMG_RE.sub(sub, md), images, problems


# ---------- 飞书接口 ----------

class Api:
    """以你本人身份调飞书；测试里换成假的。"""

    def post(self, path: str, body: dict, params: dict | None = None) -> dict:
        return fu._call("POST", path, params, body, fu.http_json)

    def patch(self, path: str, body: dict, params: dict | None = None) -> dict:
        return fu._call("PATCH", path, params, body, fu.http_json)

    def upload_image(self, path: Path, block_id: str, doc_id: str) -> str:
        data = path.read_bytes()
        fields = {"file_name": path.name, "parent_type": "docx_image", "parent_node": block_id,
                  "size": str(len(data)), "extra": json.dumps({"drive_route_token": doc_id})}
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                     f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            fu.API_BASE + "/drive/v1/medias/upload_all", data=b"".join(parts), method="POST",
            headers={"Authorization": f"Bearer {fu.access_token()}",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with fu._OPENER.open(req, timeout=120) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                resp = json.loads(e.read())
            except ValueError:
                raise fu.FeishuError(f"HTTP {e.code}：上传图片") from e
        except OSError as e:
            raise fu.FeishuError(f"网络错误（{type(e).__name__}）：上传图片") from e
        if resp.get("code") != 0:
            raise fu.FeishuError(f"{resp.get('code')} {resp.get('msg')}")
        return resp["data"]["file_token"]


def convert(api, md: str) -> tuple[list[str], list[dict], dict[str, str]]:
    """Markdown → 文档块（临时 ID）。返回 (第一层块, 全部块, 图片块临时 ID → 图片网址)。"""
    data = api.post("/docx/v1/documents/blocks/convert", {"content_type": "markdown", "content": md})
    blocks = data.get("blocks") or []
    for b in blocks:  # 表格的合并信息是只读字段，带着插入会报错
        prop = (b.get("table") or {}).get("property")
        if isinstance(prop, dict):
            prop.pop("merge_info", None)
    imgs = {x["block_id"]: x.get("image_url", "") for x in data.get("block_id_to_image_urls") or []}
    return list(data.get("first_level_block_ids") or []), blocks, imgs


def _subtree(bid: str, kids: dict[str, list[str]]) -> list[str]:
    out, stack = [], [bid]
    while stack:
        x = stack.pop()
        out.append(x)
        stack.extend(kids.get(x, []))
    return out


def insert_blocks(api, doc_id: str, first: list[str], blocks: list[dict]) -> dict[str, str]:
    """按顺序追加到文档末尾；块太多分几次。返回 临时 ID → 真实 ID。"""
    by_id = {b["block_id"]: b for b in blocks}
    kids = {b["block_id"]: list(b.get("children") or []) for b in blocks}
    groups, cur, n = [], [], 0
    for fid in first:
        size = len(_subtree(fid, kids))
        if cur and n + size > MAX_BLOCKS_PER_CALL:
            groups.append(cur)
            cur, n = [], 0
        cur.append(fid)
        n += size
    if cur:
        groups.append(cur)
    relations: dict[str, str] = {}
    for g in groups:
        ids = [x for fid in g for x in _subtree(fid, kids)]
        data = api.post(f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/descendant",
                        {"children_id": g, "index": -1, "descendants": [by_id[x] for x in ids if x in by_id]},
                        {"document_revision_id": -1, "client_token": str(uuid.uuid4())})
        for r in data.get("block_id_relations") or []:
            relations[r["temporary_block_id"]] = r["block_id"]
    return relations


def fill_images(api, doc_id: str, img_blocks: dict[str, str], images: dict[str, Path],
                relations: dict[str, str], sleep=time.sleep) -> list[str]:
    problems = []
    for temp, url in img_blocks.items():
        path, real = images.get(url), relations.get(temp)
        if not path:
            continue
        if not real:
            problems.append(f"{path.name} 没找到对应的图片位置")
            continue
        try:
            token = api.upload_image(path, real, doc_id)
            api.patch(f"/docx/v1/documents/{doc_id}/blocks/{real}", {"replace_image": {"token": token}},
                      {"document_revision_id": -1})
        except fu.FeishuError as e:
            problems.append(f"{path.name} 上传失败（{e}）")
        sleep(CALL_GAP_SEC)
    return problems


def create_doc(api, title: str, folder: str) -> str:
    body = {"title": title}
    if folder:
        body["folder_token"] = folder
    return api.post("/docx/v1/documents", body)["document"]["document_id"]


def doc_url(doc_id: str) -> str:
    """feishu.cn/docx/<ID> 会自动跳到你组织的域名（查真实网址要多开一项权限，不值得）。"""
    return f"https://feishu.cn/docx/{doc_id}"


# ---------- 已建文档的记录 ----------

def load_registry(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_registry(path: Path, reg: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def find_target(reg: dict, target: str) -> str:
    """append_to 可以是链接、文档 ID 或标题；只认 AgentOS 自己建的文档。"""
    m = DOC_ID_RE.search(target)
    if m and m.group(1) in reg:
        return m.group(1)
    same = [k for k, v in reg.items() if v.get("title") == target]
    if same:
        return max(same, key=lambda k: reg[k].get("created", 0))
    recent = sorted(reg.values(), key=lambda v: -v.get("created", 0))[:5]
    hint = "、".join(f"《{v['title']}》" for v in recent) or "还没有"
    raise DocError(f"没找到 AgentOS 建过的「{target}」。只能往 AgentOS 自己建的文档里补内容，最近建过的：{hint}")


# ---------- 处理一份请求 ----------

def process(path: Path, runtime: Path, registry_path: Path, folder: str, api=None, now=None, sleep=time.sleep) -> str:
    """处理一份请求，返回要推给你的一句话。失败抛 DocError / FeishuError。"""
    api = api or Api()
    req = load_request(path, runtime)
    reg = load_registry(registry_path)
    md, images, problems = prepare_markdown(req["markdown"], runtime, req["md_path"].parent)
    if req["append_to"]:
        doc_id = find_target(reg, req["append_to"])
        title = reg[doc_id]["title"]
    else:
        title = req["title"]
        doc_id = create_doc(api, title, folder)
        reg[doc_id] = {"title": title, "created": now or time.time(), "url": ""}
        save_registry(registry_path, reg)  # 先记下：后面出错也能接着往里补
    first, blocks, img_blocks = convert(api, md)
    relations = insert_blocks(api, doc_id, first, blocks)
    problems += fill_images(api, doc_id, img_blocks, images, relations, sleep)
    if not reg[doc_id].get("url"):
        reg[doc_id]["url"] = doc_url(doc_id)
    reg[doc_id]["updated"] = now or time.time()
    save_registry(registry_path, reg)
    url = reg[doc_id]["url"]
    head = f"📄 已把内容补进《{title}》" if req["append_to"] else f"📄 已建好飞书文档《{title}》"
    tail = ("\n⚠️ " + "；".join(problems)) if problems else ""
    return f"{head}：{url}{tail}"
