"""用 GitHub Git Data API 原子推送一个提交。

为什么需要它：这台机器访问 github.com:443 经常不通，git push 无法工作，
但 api.github.com 可达。所以改用 API 走 blobs -> tree -> commit -> update ref。

两个关键设计：

1. 文件内容从 git 对象库读取（git show HEAD:path），而不是工作区。
   因为 .gitattributes 声明了 eol=lf，工作区可能是 CRLF，直接上传会污染换行符。

2. 用 blob SHA 比对本地与远端，而不是 git diff。
   远端提交可能也是通过 API 创建的，本地仓库并不知道它，
   两者没有共同祖先，git diff 会直接 exit 128。
   而 blob SHA 是内容哈希，同一套算法，直接比就知道哪些文件真的不同。

用法：
    python tools/push_via_api.py
    GH_TOKEN=xxx python tools/push_via_api.py   # 手动指定 token
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

OWNER = "kaynai1"
REPO = "cfopt"
BRANCH = "main"
API = "https://api.github.com"


# ---------------------------------------------------------------- 认证
def get_token():
    """读取 GitHub token。

    不能用 `git credential fill`：当 github.com 不可达时，凭据管理器会
    转为交互式认证而无限挂起（实测卡死）。改为直接读 Windows 凭据库。
    """
    for env in ("GH_TOKEN", "GITHUB_TOKEN"):
        v = os.environ.get(env)
        if v:
            return v.strip()

    if sys.platform == "win32":
        for target in ("git:https://kaynai1@github.com",
                       "git:https://github.com"):
            tok = _win_credential(target)
            if tok:
                return tok

    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=10, check=True).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    raise SystemExit("找不到 GitHub token。请设置 GH_TOKEN 环境变量后重试。")


def _win_credential(target):
    """通过 advapi32 的 CredReadW 读取 Windows 凭据库。"""
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    # Unicode 版本函数名带 W 后缀，不加会报 function not found
    advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                   wintypes.DWORD,
                                   ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]

    ptr = ctypes.POINTER(CREDENTIAL)()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        if cred.CredentialBlobSize == 0:
            return None
        buf = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return buf.decode("utf-16-le").rstrip("\x00")
    finally:
        advapi32.CredFree(ptr)


# ---------------------------------------------------------------- HTTP
def api(method, path, token, payload=None):
    url = f"{API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "cfopt-push")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"{method} {path} -> {e.code}\n{e.read().decode()[:600]}")


# ---------------------------------------------------------------- git
def git(*args):
    """执行 git。必须显式 UTF-8：Windows 默认 GBK 读输出，中文会解码失败。"""
    return subprocess.run(
        ["git"] + list(args), capture_output=True, check=True,
        encoding="utf-8", errors="replace").stdout


def local_tree(rev="HEAD"):
    """本地提交的 path -> blob sha。

    git ls-tree 的 blob sha 就是内容 SHA-1，和 GitHub 同一套算法，
    可以直接比对，不必下载内容。
    """
    out = git("ls-tree", "-r", rev)
    tree = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[1] == "blob":
            tree[parts[3]] = parts[2]
    return tree


def remote_tree(token, tree_sha):
    """GitHub 上某棵树的 path -> blob sha（递归）。"""
    data = api("GET",
               f"/repos/{OWNER}/{REPO}/git/trees/{tree_sha}?recursive=1", token)
    if data.get("truncated"):
        raise SystemExit("远端树过大被截断，需改用分批比对")
    return {i["path"]: i["sha"] for i in data.get("tree", [])
            if i["type"] == "blob"}


# ---------------------------------------------------------------- 主流程
def main():
    token = get_token()

    head = api("GET", f"/repos/{OWNER}/{REPO}/commits/{BRANCH}", token)
    parent_sha = head["sha"]
    base_tree = head["commit"]["tree"]["sha"]
    print(f"远端 HEAD: {parent_sha[:7]}")

    local_head = git("rev-parse", "HEAD").strip()
    print(f"本地 HEAD: {local_head[:7]}")

    local = local_tree(local_head)
    remote = remote_tree(token, base_tree)

    changed = [p for p, sha in sorted(local.items())
               if remote.get(p) != sha]
    deleted = [p for p in sorted(remote) if p not in local]

    if not changed and not deleted:
        print("内容已完全一致，无需推送（仅提交历史不同）")
        return 0
    print(f"待推送 {len(changed)} 个文件，删除 {len(deleted)} 个")

    entries = []
    for path in changed:
        blob_bytes = subprocess.run(
            ["git", "show", f"{local_head}:{path}"],
            capture_output=True, check=True).stdout
        blob = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs", token, {
            "content": base64.b64encode(blob_bytes).decode(),
            "encoding": "base64",
        })
        entries.append({"path": path, "mode": "100644", "type": "blob",
                        "sha": blob["sha"]})
        print(f"  blob {path} ({len(blob_bytes)} bytes)")

    for path in deleted:
        entries.append({"path": path, "mode": "100644", "type": "blob",
                        "sha": None})
        print(f"  del  {path}")

    tree = api("POST", f"/repos/{OWNER}/{REPO}/git/trees", token, {
        "base_tree": base_tree, "tree": entries,
    })

    msg = git("log", "-1", "--pretty=%B", local_head).strip()
    commit = api("POST", f"/repos/{OWNER}/{REPO}/git/commits", token, {
        "message": msg, "tree": tree["sha"], "parents": [parent_sha],
    })
    print(f"新提交: {commit['sha'][:7]}")

    api("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}", token, {
        "sha": commit["sha"], "force": False,
    })
    print(f"已更新 {BRANCH} -> {commit['sha'][:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
