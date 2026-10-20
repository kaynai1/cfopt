# -*- coding: utf-8 -*-
"""单元测试：不依赖外网，全部用本地构造的报文验证。"""
import json
import os
import struct
import unittest

from cfopt.dnssrv import DnsServer, build_answer, parse_question, parse_qname
from cfopt.probe import expand_ranges, nets_from_results


def make_query(name, qtype=1, tid=b"\x12\x34"):
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return (tid + b"\x01\x00" + struct.pack("!HHHH", 1, 0, 0, 0)
            + q + struct.pack("!HH", qtype, 1))


class TestDnsParsing(unittest.TestCase):
    def test_parse_qname(self):
        pkt = make_query("ai2.kdns.fr")
        name, off = parse_qname(pkt, 12)
        self.assertEqual(name, "ai2.kdns.fr")
        # ai2(1+3) + kdns(1+4) + fr(1+2) + 结束字节 0x00 = 13
        self.assertEqual(off, 12 + 13)

    def test_parse_question_a(self):
        pkt = make_query("ai2.kdns.fr", 1)
        name, qtype, end = parse_question(pkt)
        self.assertEqual(name, "ai2.kdns.fr")
        self.assertEqual(qtype, 1)
        # 12 字节头 + 13 字节域名 + 4 字节 QTYPE/QCLASS = 问题段结束位置
        self.assertEqual(end, 12 + 13 + 4)
        self.assertEqual(end, len(pkt))

    def test_parse_question_aaaa(self):
        pkt = make_query("ai2.kdns.fr", 28)
        _, qtype, _ = parse_question(pkt)
        self.assertEqual(qtype, 28)

    def test_case_insensitive(self):
        pkt = make_query("AI2.KDNS.FR")
        name, _ = parse_qname(pkt, 12)
        self.assertEqual(name, "ai2.kdns.fr")

    def test_truncated_raises(self):
        with self.assertRaises(ValueError):
            parse_qname(b"\x00" * 12, 12)


class TestBuildAnswer(unittest.TestCase):
    def test_two_a_records(self):
        pkt = make_query("ai2.kdns.fr", 1)
        _, _, end = parse_question(pkt)
        resp = build_answer(pkt, end, 1, ["104.17.183.4", "104.17.146.2"])

        tid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", resp[:12])
        self.assertEqual(tid, 0x1234)
        self.assertTrue(flags & 0x8000, "QR 位必须置 1")
        self.assertTrue(flags & 0x0080, "RA 位必须置 1")
        self.assertEqual(qd, 1)
        self.assertEqual(an, 2)
        self.assertEqual(ns, 0)

        # 校验第一条记录的 IP
        first = resp[end + 2:]
        rtype, rclass, ttl, rdlen = struct.unpack("!HHIH", first[:10])
        self.assertEqual(rtype, 1)
        self.assertEqual(rclass, 1)
        self.assertEqual(ttl, 300)
        self.assertEqual(rdlen, 4)
        self.assertEqual(".".join(str(b) for b in first[10:14]), "104.17.183.4")

    def test_aaaa_empty_answer(self):
        """AAAA 查询返回 0 条记录，促使客户端回退 IPv4。"""
        pkt = make_query("ai2.kdns.fr", 28)
        _, _, end = parse_question(pkt)
        resp = build_answer(pkt, end, 28, [])
        _, _, qd, an, _, _ = struct.unpack("!HHHHHH", resp[:12])
        self.assertEqual(qd, 1)
        self.assertEqual(an, 0)

    def test_preserves_rd_flag(self):
        """原始查询带 RD，应答必须保留，否则部分客户端会重试。"""
        pkt = make_query("ai2.kdns.fr")
        self.assertTrue(struct.unpack("!H", pkt[2:4])[0] & 0x0100)
        _, _, end = parse_question(pkt)
        resp = build_answer(pkt, end, 1, ["1.2.3.4"])
        self.assertTrue(struct.unpack("!H", resp[2:4])[0] & 0x0100)


class TestServerLogic(unittest.TestCase):
    def setUp(self):
        self.srv = DnsServer(
            "127.0.0.1", 0, [("223.5.5.5", 53)],
            ["ai2.kdns.fr"], lambda: ["104.17.183.4"],
            log=lambda m: None)

    def test_exact_match(self):
        self.assertTrue(self.srv._matches("ai2.kdns.fr"))

    def test_subdomain_match(self):
        self.assertTrue(self.srv._matches("www.ai2.kdns.fr"))

    def test_no_false_positive(self):
        """xai2.kdns.fr 不能被误匹配（后缀必须以 . 分隔）。"""
        self.assertFalse(self.srv._matches("xai2.kdns.fr"))
        self.assertFalse(self.srv._matches("ai2.kdns.fr.evil.com"))

    def test_short_packet_returns_none(self):
        self.assertIsNone(self.srv.handle(b"\x00\x01"))

    def test_empty_ip_list_falls_back_to_upstream(self):
        """回归测试：没有优选 IP 时必须转发上游，不能返回空应答。

        曾经的 bug：无缓存时返回 ANCOUNT=0 的空应答，
        导致域名直接解析失败，站点打不开。
        """
        srv = DnsServer("127.0.0.1", 0, [("223.5.5.5", 53)],
                        ["ai2.kdns.fr"], lambda: [], log=lambda m: None)
        forwarded = {"called": False}

        def fake_forward(packet):
            forwarded["called"] = True
            return b"upstream-response"

        srv._forward = fake_forward
        resp = srv.handle(make_query("ai2.kdns.fr", 1))
        self.assertTrue(forwarded["called"], "必须调用上游转发")
        self.assertEqual(resp, b"upstream-response")

    def test_aaaa_always_empty_without_upstream(self):
        """AAAA 主动返回空应答，且不消耗上游查询。"""
        srv = DnsServer("127.0.0.1", 0, [("223.5.5.5", 53)],
                        ["ai2.kdns.fr"], lambda: ["104.17.183.4"],
                        log=lambda m: None)
        srv._forward = lambda p: b"should-not-happen"
        resp = srv.handle(make_query("ai2.kdns.fr", 28))
        self.assertIsNotNone(resp)
        _, _, qd, an, _, _ = struct.unpack("!HHHHHH", resp[:12])
        self.assertEqual(qd, 1)
        self.assertEqual(an, 0)


class TestExpandRanges(unittest.TestCase):
    def test_per_net_one(self):
        ips = expand_ranges(["104.17.183.0/24"], 1)
        self.assertEqual(len(ips), 1)
        self.assertEqual(ips[0], "104.17.183.1")

    def test_per_net_three(self):
        ips = expand_ranges(["104.17.183.0/24"], 3)
        self.assertEqual(ips, ["104.17.183.1", "104.17.183.2", "104.17.183.3"])

    def test_small_prefix(self):
        ips = expand_ranges(["104.17.0.0/16"], 1)
        self.assertEqual(len(ips), 256)  # 256 个 /24，每段 1 个

    def test_invalid_cidr_raises(self):
        with self.assertRaises(ValueError):
            expand_ranges(["not-a-cidr"], 1)


class TestNetsFromResults(unittest.TestCase):
    def test_groups_by_24_and_sorts(self):
        results = [
            {"ip": "104.17.183.4", "colo": "HKG", "connect_ms": 60},
            {"ip": "104.17.183.9", "colo": "HKG", "connect_ms": 65},
            {"ip": "104.16.255.1", "colo": "HKG", "connect_ms": 55},
            {"ip": "104.21.36.213", "colo": "SJC", "connect_ms": 180},
        ]
        nets = nets_from_results(results, "HKG")
        self.assertEqual(nets, ["104.16.255.0/24", "104.17.183.0/24"])

    def test_limit(self):
        results = [{"ip": f"104.17.{i}.1", "colo": "HKG", "connect_ms": i}
                   for i in range(10)]
        self.assertEqual(len(nets_from_results(results, "HKG", limit=3)), 3)

    def test_empty_when_no_match(self):
        self.assertEqual(nets_from_results([], "HKG"), [])


class TestBindFailure(unittest.TestCase):
    """回归测试：端口占用时必须显式失败，不能静默崩溃。

    曾经的 bug：UDP 绑定失败时线程抛异常直接死掉，主进程照常运行，
    日志里只有"TCP 监听"一行。用户以为部署成功，实际 DNS 从未生效。
    """

    def test_bind_failure_is_reported(self):
        import socket

        # 先占住一个端口
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", 0))
        port = blocker.getsockname()[1]
        try:
            srv = DnsServer("127.0.0.1", port, [("223.5.5.5", 53)],
                            ["a.com"], lambda: ["1.2.3.4"],
                            log=lambda m: None)
            srv.start(wait=1.0)
            self.assertFalse(srv.ok, "端口被占用时 ok 必须为 False")
            self.assertTrue(srv.udp_error, "必须记录错误原因")
            self.assertIn("绑定失败", srv.udp_error)
        finally:
            blocker.close()

    def test_ok_true_when_bound(self):
        srv = DnsServer("127.0.0.1", 0, [("223.5.5.5", 53)],
                        ["a.com"], lambda: ["1.2.3.4"], log=lambda m: None)
        srv.start(wait=1.5)
        try:
            # 端口 0 由系统分配，正常应该绑定成功
            self.assertTrue(srv.ok)
            self.assertEqual(srv.udp_error, "")
        finally:
            srv.stop()


class TestSystemHosts(unittest.TestCase):
    """MODE=hosts 时对系统 hosts 的写入必须幂等，不能累积区块。"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "hosts")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _cfg(self):
        from cfopt.main import Config
        cfg = Config()
        cfg.target_host = "ai2.kdns.fr"
        cfg.system_hosts = self.path
        cfg.mode = "hosts"
        return cfg

    def _write(self, content):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(content)

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_creates_block_in_empty_file(self):
        from cfopt.main import write_system_hosts
        write_system_hosts(self._cfg(), [("ai2.kdns.fr", ["104.17.183.4"])])
        text = self._read()
        self.assertIn("# >>> cfopt-managed >>>", text)
        self.assertIn("104.17.183.4\tai2.kdns.fr", text)
        self.assertIn("# <<< cfopt-managed <<<", text)

    def test_preserves_existing_lines(self):
        from cfopt.main import write_system_hosts
        self._write("127.0.0.1\tlocalhost\n192.168.1.50\tfnos.local\n")
        write_system_hosts(self._cfg(), [("ai2.kdns.fr", ["104.17.183.4"])])
        text = self._read()
        self.assertIn("127.0.0.1\tlocalhost", text)
        self.assertIn("192.168.1.50\tfnos.local", text)

    def test_replaces_old_block_not_appends(self):
        """连续写两次，第二次必须替换而不是追加，否则文件会越来越长。"""
        from cfopt.main import write_system_hosts
        write_system_hosts(self._cfg(), [("ai2.kdns.fr", ["1.1.1.1"])])
        write_system_hosts(self._cfg(), [("ai2.kdns.fr", ["104.17.183.4"])])
        text = self._read()
        self.assertEqual(text.count("# >>> cfopt-managed >>>"), 1)
        self.assertNotIn("1.1.1.1", text)
        self.assertIn("104.17.183.4", text)

    def test_multiple_ips(self):
        from cfopt.main import write_system_hosts
        write_system_hosts(self._cfg(), [("ai2.kdns.fr", ["104.17.183.4", "104.17.146.2"])])
        text = self._read()
        self.assertIn("104.17.183.4\tai2.kdns.fr", text)
        self.assertIn("104.17.146.2\tai2.kdns.fr", text)

    def test_falls_back_when_replace_fails(self):
        """模拟 Docker 单文件挂载：os.replace 抛 EBUSY 时必须原地写入。

        这是真实场景：compose 里挂 `- /etc/hosts:/hosts/etc-hosts`，
        挂载点无法 rename，只能退化为非原子的原地写。
        """
        import unittest.mock as mock
        from cfopt.main import write_system_hosts

        self._write("127.0.0.1\tlocalhost\n")
        cfg = self._cfg()

        real_replace = os.replace

        def fake_replace(src, dst):
            raise OSError(16, "Device or resource busy")

        with mock.patch("cfopt.main.os.replace", side_effect=fake_replace):
            write_system_hosts(cfg, [("ai2.kdns.fr", ["104.17.183.4"])])

        text = self._read()
        self.assertIn("104.17.183.4\tai2.kdns.fr", text, "必须退回原地写入")
        self.assertIn("127.0.0.1\tlocalhost", text, "原有内容不能丢")
        self.assertFalse(os.path.exists(self.path + ".cfopt.tmp"),
                         "临时文件应被清理")
        self.assertIs(os.replace, real_replace)


class TestDomainStore(unittest.TestCase):
    """多域名配置的增删改查与落盘。"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "domains.json")
        from cfopt.store import DomainStore
        self.store = DomainStore(self.path, log=lambda m: None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_and_list(self):
        d = self.store.add("ai2.kdns.fr")
        self.assertEqual(d.host, "ai2.kdns.fr")
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(d.colos, ["HKG"])

    def test_add_normalizes_host(self):
        d = self.store.add("  AI2.KDNS.FR.  ")
        self.assertEqual(d.host, "ai2.kdns.fr", "应统一小写并去掉末尾点")

    def test_duplicate_rejected(self):
        self.store.add("a.com")
        with self.assertRaises(ValueError):
            self.store.add("a.com")

    def test_invalid_host_rejected(self):
        for bad in ["", "  ", "nodot", "a..b", "-bad.com", "a b.com"]:
            with self.assertRaises(ValueError, msg=f"{bad!r} 应被拒绝"):
                self.store.add(bad)

    def test_wildcard_allowed(self):
        d = self.store.add("*.example.com")
        self.assertEqual(d.host, "*.example.com")

    def test_remove(self):
        d = self.store.add("a.com")
        self.assertTrue(self.store.remove(d.id))
        self.assertEqual(len(self.store.list()), 0)
        self.assertFalse(self.store.remove(d.id), "重复删除应返回 False")

    def test_persistence(self):
        """写盘后重新加载，配置和扫描结果都要保留。"""
        d = self.store.add("a.com", colos=["HKG", "NRT"], top_n=3)
        d.apply_scan(["1.2.3.4"], {"HKG": 10}, ["104.17.0.0/24"])

        from cfopt.store import DomainStore
        store2 = DomainStore(self.path, log=lambda m: None)
        items = store2.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].host, "a.com")
        self.assertEqual(items[0].colos, ["HKG", "NRT"])
        self.assertEqual(items[0].top_n, 3)
        self.assertEqual(items[0].get_ips(), ["1.2.3.4"])
        self.assertEqual(items[0].nets, ["104.17.0.0/24"])

    def test_by_host(self):
        self.store.add("a.com")
        self.assertIsNotNone(self.store.by_host("A.COM"))
        self.assertIsNone(self.store.by_host("b.com"))

    def test_enabled_filter(self):
        a = self.store.add("a.com")
        self.store.add("b.com")
        self.store.update(a.id, enabled=False)
        self.assertEqual(len(self.store.enabled()), 1)
        self.assertEqual(self.store.enabled()[0].host, "b.com")

    def test_apply_scan_sets_state(self):
        d = self.store.add("a.com")
        self.assertFalse(d.last_scan_ok)
        d.apply_scan(["1.1.1.1"], {"HKG": 5})
        self.assertTrue(d.last_scan_ok)
        self.assertEqual(d.last_error, "")
        self.assertGreater(d.last_scan_at, 0)

    def test_mark_failed_keeps_old_ips(self):
        """扫描失败不能清空已有结果，否则域名会突然解析不了。"""
        d = self.store.add("a.com")
        d.apply_scan(["1.1.1.1"], {"HKG": 5})
        d.mark_failed("超时")
        self.assertFalse(d.last_scan_ok)
        self.assertEqual(d.get_ips(), ["1.1.1.1"], "旧 IP 必须保留")

    def test_corrupt_file_starts_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        from cfopt.store import DomainStore
        store = DomainStore(self.path, log=lambda m: None)
        self.assertEqual(len(store.list()), 0)

    def test_env_migration(self):
        d = self.store.ensure_from_env("old.com", ["HKG"], 2, 2, 21600)
        self.assertIsNotNone(d)
        self.assertEqual(d.host, "old.com")
        # 已有其他域名时不再迁移
        self.store.add("other.com")
        self.assertIsNone(self.store.ensure_from_env("new.com", ["HKG"], 2, 2, 21600))


class TestWebRoutes(unittest.TestCase):
    """一个端口访问所有页面 + REST API 行为。"""

    def setUp(self):
        """每个测试用独立的 store 和独立端口，避免测试之间互相污染。"""
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer

        from cfopt.main import Config
        from cfopt.store import DomainStore
        from cfopt import web

        self.tmpdir = tempfile.mkdtemp()
        cfg = Config()
        cfg.data_dir = self.tmpdir
        cfg.mode = "hosts"
        cfg.http_port = 0

        self.store = DomainStore(os.path.join(self.tmpdir, "domains.json"),
                                 log=lambda m: None)
        d = self.store.add("ai2.kdns.fr")
        # 模拟一次真实扫描的结果：含候选节点明细
        d.apply_scan(
            ["104.16.255.1"],
            {"HKG": 316, "LAX": 1465},
            nodes=[
                {"ip": "104.16.255.1", "colo": "HKG",
                 "connect_ms": 61.0, "ttfb_ms": 593.0},
                {"ip": "104.17.21.2", "colo": "HKG",
                 "connect_ms": 72.0, "ttfb_ms": 640.0},
                {"ip": "104.21.36.213", "colo": "SJC",
                 "connect_ms": 187.0, "ttfb_ms": 924.0},
            ],
            total_alive=4971,
        )

        class FakeScanner:
            def __init__(self):
                self.triggered = []

            def trigger(self, dom):
                self.triggered.append(dom.host)

            def is_running(self, did):
                return False

        self.scanner = FakeScanner()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        web.Handler.cfg = cfg
        web.Handler.store = self.store
        web.Handler.scanner = self.scanner
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _req(self, method, path, body=None, accept=None):
        import urllib.request
        import urllib.error
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        if accept:
            req.add_header("Accept", accept)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read().decode()

    # ---------------------------------------------------------- 页面
    def test_main_pages_ok(self):
        """单页设计：只有首页和状态页。"""
        for path in ["/", "/status"]:
            status, _, body = self._req("GET", path)
            self.assertEqual(status, 200, f"{path} 应 200")
            self.assertTrue(body.strip(), f"{path} 不应为空")

    def test_removed_pages_gone(self):
        """图解页和接入指南已按要求移除，应返回 404。"""
        for path in ["/diagram", "/setup", "/guide"]:
            status, _, _ = self._req("GET", path)
            self.assertEqual(status, 404, f"{path} 应已移除")

    def test_overview_shows_status_badge(self):
        """首页必须一眼看出是否已接入。"""
        _, _, body = self._req("GET", "/")
        self.assertIn("已接入", body, "需要接入状态标识")

    def test_overview_shows_node_table(self):
        """候选节点表要显示 IP、机房、延迟。"""
        _, _, body = self._req("GET", "/")
        self.assertIn("候选节点", body)
        self.assertIn("104.16.255.1", body)
        self.assertIn("HKG", body)

    def test_overview_lists_domain(self):
        _, _, body = self._req("GET", "/")
        self.assertIn("ai2.kdns.fr", body)
        self.assertIn("HKG 316", body, "机房分布应显示")

    def test_overview_has_add_form(self):
        _, _, body = self._req("GET", "/")
        self.assertIn('id="host"', body, "必须有域名输入框")
        self.assertIn("addDomain", body)

    def test_health(self):
        status, ctype, body = self._req("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertTrue(json.loads(body)["ok"])

    # ---------------------------------------------------------- API
    def test_api_list_domains(self):
        status, _, body = self._req("GET", "/api/domains")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(len(data["domains"]), 1)
        self.assertEqual(data["domains"][0]["host"], "ai2.kdns.fr")
        self.assertEqual(data["domains"][0]["ips"], ["104.16.255.1"])

    def test_api_add_domain_triggers_scan(self):
        status, _, body = self._req("POST", "/api/domains",
                                    {"host": "new.example.com", "colos": ["HKG"]})
        self.assertEqual(status, 201, body)
        self.assertEqual(json.loads(body)["domain"]["host"], "new.example.com")
        self.assertIn("new.example.com", self.scanner.triggered,
                      "新增后应立即触发扫描")

    def test_api_add_duplicate_returns_error(self):
        status, _, body = self._req("POST", "/api/domains",
                                    {"host": "ai2.kdns.fr"})
        self.assertGreaterEqual(status, 400)
        self.assertIn("已存在", json.loads(body)["error"])

    def test_api_add_invalid_host(self):
        status, _, body = self._req("POST", "/api/domains", {"host": "nodot"})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_api_add_short_interval_rejected(self):
        status, _, body = self._req("POST", "/api/domains",
                                    {"host": "fast.com", "interval": 60})
        self.assertEqual(status, 400)
        self.assertIn("600", json.loads(body)["error"])

    def test_api_delete(self):
        s, _, b = self._req("POST", "/api/domains", {"host": "tmp.example.com"})
        did = json.loads(b)["domain"]["id"]
        status, _, _ = self._req("DELETE", f"/api/domains/{did}")
        self.assertEqual(status, 200)
        status, _, _ = self._req("DELETE", f"/api/domains/{did}")
        self.assertEqual(status, 404, "重复删除应 404")

    def test_api_patch(self):
        s, _, b = self._req("POST", "/api/domains", {"host": "patch.example.com"})
        did = json.loads(b)["domain"]["id"]
        status, _, body = self._req("PATCH", f"/api/domains/{did}",
                                    {"colos": ["NRT"], "top_n": 3})
        self.assertEqual(status, 200)
        dom = json.loads(body)["domain"]
        self.assertEqual(dom["colos"], ["NRT"])
        self.assertEqual(dom["top_n"], 3)

    def test_api_patch_invalid_topn(self):
        s, _, b = self._req("POST", "/api/domains", {"host": "bad.example.com"})
        did = json.loads(b)["domain"]["id"]
        status, _, body = self._req("PATCH", f"/api/domains/{did}", {"top_n": 99})
        self.assertEqual(status, 400)

    def test_status_negotiates_json(self):
        _, ctype, body = self._req("GET", "/status", accept="application/json")
        self.assertIn("application/json", ctype)
        self.assertIn("domains", json.loads(body))


class TestScannerLogic(unittest.TestCase):
    """扫描调度器的纯逻辑部分，不发真实请求。"""

    def test_is_running_default_false(self):
        from cfopt.scanner import Scanner
        from cfopt.store import DomainStore
        import tempfile
        d = tempfile.mkdtemp()
        try:
            store = DomainStore(os.path.join(d, "d.json"), log=lambda m: None)
            s = Scanner(store, log=lambda m: None)
            self.assertFalse(s.is_running("nope"))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_nets_reused_for_fast_scan(self):
        """有网段缓存时应走快速模式，不再展开全部 CF 网段。"""
        from cfopt.scanner import Scanner
        from cfopt.store import DomainStore
        import tempfile
        d = tempfile.mkdtemp()
        try:
            store = DomainStore(os.path.join(d, "d.json"), log=lambda m: None)
            dom = store.add("a.com")
            dom.nets = ["104.17.183.0/24"]
            dom.nets_colos = ["HKG"]
            s = Scanner(store, log=lambda m: None)
            captured = {}

            def fake_scan(ips, host, workers=0, timeout=0,
                          progress=None, retries=0, refine_top=0):
                captured["ips"] = ips
                # 模拟一次进度回调，验证 scanner 把进度传下去了
                if progress:
                    progress(len(ips), len(ips), 0)
                return []

            import cfopt.scanner as mod
            orig = mod.scan
            mod.scan = fake_scan
            try:
                s.scan_one(dom)
            finally:
                mod.scan = orig
            self.assertTrue(all(ip.startswith("104.17.183.") for ip in captured["ips"]),
                            "有缓存时只应扫该网段")
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class TestCloudflareOwnership(unittest.TestCase):
    """添加域名前的 CF 归属校验：填错域名要立刻提示，而不是白扫十几分钟。"""

    def test_cf_ip_detection(self):
        from cfopt.probe import is_cloudflare_ip
        # Cloudflare 官方段内的地址
        for ip in ["104.16.0.1", "172.64.145.2", "162.159.0.4", "198.41.192.57"]:
            self.assertTrue(is_cloudflare_ip(ip), f"{ip} 应识别为 CF")
        # 明显不属于 CF 的
        for ip in ["36.152.44.93", "183.194.238.19", "8.8.8.8", "1.1.1.1"]:
            self.assertFalse(is_cloudflare_ip(ip), f"{ip} 不应识别为 CF")

    def test_invalid_ip(self):
        from cfopt.probe import is_cloudflare_ip
        self.assertFalse(is_cloudflare_ip("not-an-ip"))
        self.assertFalse(is_cloudflare_ip(""))

    def test_unresolvable_returns_none(self):
        """解析失败返回 None，调用方不应因此拒绝用户。"""
        from cfopt.probe import check_domain_on_cloudflare
        ok, detail = check_domain_on_cloudflare(
            "definitely-not-exist-xyz-98765.invalid")
        self.assertIsNone(ok)
        self.assertIn("失败", detail)

    def test_wildcard_stripped(self):
        """通配符域名要能正常查（去掉 *. 前缀）。"""
        from cfopt.probe import check_domain_on_cloudflare
        ok, _ = check_domain_on_cloudflare("*.definitely-not-exist-xyz-98765.invalid")
        self.assertIsNone(ok)


class TestApiRejectsNonCloudflare(unittest.TestCase):
    """API 层：非 CF 域名必须被拒绝。"""

    def setUp(self):
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer

        from cfopt.main import Config
        from cfopt.store import DomainStore
        from cfopt import web

        self.tmpdir = tempfile.mkdtemp()
        cfg = Config()
        cfg.data_dir = self.tmpdir
        cfg.mode = "hosts"
        cfg.http_port = 0

        self.store = DomainStore(os.path.join(self.tmpdir, "domains.json"),
                                 log=lambda m: None)

        class FakeScanner:
            def trigger(self, dom):
                pass

            def is_running(self, did):
                return False

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        web.Handler.cfg = cfg
        web.Handler.store = self.store
        web.Handler.scanner = FakeScanner()
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _post(self, body):
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/domains",
            data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_rejects_non_cloudflare_domain(self):
        """www.baidu.com 不在 CF 上，必须被拒绝并说明原因。"""
        status, body = self._post({"host": "www.baidu.com"})
        self.assertEqual(status, 400)
        self.assertIn("没有接入 Cloudflare", body["error"])
        self.assertEqual(len(self.store.list()), 0, "不应写入配置")

    def test_rejects_invalid_format_first(self):
        """格式非法时先报格式错误，不做 DNS 查询。"""
        status, body = self._post({"host": "nodot"})
        self.assertEqual(status, 400)
        self.assertIn("格式不合法", body["error"])


class TestScanSpeed(unittest.TestCase):
    """扫描性能相关的回归测试。

    背景：早期版本 timeout=4 + retries=2，死 IP 单个要 12.5 秒，
    11912 个候选跑 13 分钟，用户以为卡死了。
    """

    def test_dead_ip_not_retried_by_default(self):
        """默认必须不重试：对死 IP 重试纯浪费，耗时翻倍。"""
        import inspect
        from cfopt.probe import scan
        sig = inspect.signature(scan)
        self.assertEqual(sig.parameters["retries"].default, 0,
                         "scan 默认不能带重试，否则死 IP 会拖慢整体")
        self.assertLessEqual(sig.parameters["timeout"].default, 3.0,
                             "默认超时应 ≤3 秒")

    def test_probe_default_retries_for_single_use(self):
        """单点探测仍保留重试，因为只测一两个 IP 时准确性更重要。"""
        import inspect
        from cfopt.probe import probe
        sig = inspect.signature(probe)
        self.assertEqual(sig.parameters["retries"].default, 2)

    def test_progress_callback_called(self):
        """进度回调必须被调用，前端靠它显示百分比。"""
        from cfopt import probe as pmod

        calls = []
        real = pmod.probe

        def fake(ip, host, port=443, timeout=4.0, retries=0):
            return {"ip": ip, "colo": "HKG", "connect_ms": 50.0,
                    "tls_ms": 80.0, "ttfb_ms": 200.0, "status": 200}

        pmod.probe = fake
        try:
            pmod.scan(["1.1.1.1", "2.2.2.2"], "a.com", workers=2,
                      progress=lambda d, t, a: calls.append((d, t, a)))
        finally:
            pmod.probe = real

        self.assertTrue(calls, "进度回调一次都没被调用")
        self.assertEqual(calls[-1][0], 2, "最后一次应是 done=total")
        self.assertEqual(calls[-1][1], 2)


class TestProgressState(unittest.TestCase):
    """进度状态在 Domain 上的存取与快照。"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        from cfopt.store import DomainStore
        self.store = DomainStore(os.path.join(self.tmpdir, "d.json"),
                                 log=lambda m: None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_progress_by_default(self):
        d = self.store.add("a.com")
        self.assertIsNone(d.snapshot()["progress"])

    def test_set_and_clear(self):
        d = self.store.add("a.com")
        d.set_progress(500, 1000, 200)
        p = d.snapshot()["progress"]
        self.assertIsNotNone(p)
        self.assertEqual(p["done"], 500)
        self.assertEqual(p["total"], 1000)
        self.assertEqual(p["alive"], 200)
        self.assertEqual(p["pct"], 50)

        d.clear_progress()
        self.assertIsNone(d.snapshot()["progress"])

    def test_pct_zero_when_total_zero(self):
        """total 为 0 时不能除零崩溃。"""
        d = self.store.add("a.com")
        d.set_progress(0, 0, 0)
        self.assertEqual(d.snapshot()["progress"]["pct"], 0)

    def test_progress_not_persisted(self):
        """进度是内存态，不该写进 domains.json。"""
        d = self.store.add("a.com")
        d.set_progress(10, 100, 5)
        with open(self.store.path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("progress", raw, "进度不应落盘")


class TestNetsCacheInvalidation(unittest.TestCase):
    """网段缓存必须和目标机房绑定。

    真实故障：把机房从 NRT 改成 HKG 后，界面一直提示
    「无 HKG 节点，实际分布 NRT,SIN,LAX,AMS,FRA」。
    原因是缓存只按当时命中的机房生成，且一旦存在就永远只扫这些段，
    所以无论怎么扫都出不来 HKG。
    """

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        from cfopt.store import DomainStore
        self.store = DomainStore(os.path.join(self.tmpdir, "d.json"),
                                 log=lambda m: None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _cache_valid(self, domain):
        """复刻 scanner 里的判定逻辑。"""
        cache_colos = getattr(domain, "nets_colos", None) or []
        return bool(domain.nets) and cache_colos == list(domain.colos)

    def test_legacy_cache_without_marker_is_discarded(self):
        """旧版本留下的缓存没有 nets_colos，必须作废重扫。"""
        d = self.store.add("a.com", colos=["HKG"])
        d.nets = ["104.16.0.0/24"]
        d.nets_colos = []
        self.assertFalse(self._cache_valid(d))

    def test_cache_from_other_colo_is_discarded(self):
        """缓存来自 NRT，目标改成 HKG，必须作废。"""
        d = self.store.add("a.com", colos=["HKG"])
        d.nets = ["104.16.0.0/24"]
        d.nets_colos = ["NRT"]
        self.assertFalse(self._cache_valid(d))

    def test_matching_cache_is_reused(self):
        """缓存与目标机房一致时才复用，保证快速扫描。"""
        d = self.store.add("a.com", colos=["HKG"])
        d.nets = ["104.16.0.0/24"]
        d.nets_colos = ["HKG"]
        self.assertTrue(self._cache_valid(d))

    def test_multi_colo_order_matters(self):
        """机房顺序变了也算变更（HKG,NRT 与 NRT,HKG 优先级不同）。"""
        d = self.store.add("a.com", colos=["NRT", "HKG"])
        d.nets = ["104.16.0.0/24"]
        d.nets_colos = ["HKG", "NRT"]
        self.assertFalse(self._cache_valid(d))

    def test_nets_colos_persisted(self):
        """nets_colos 要落盘，否则重启后又变成无标记的旧缓存。"""
        d = self.store.add("a.com", colos=["HKG"])
        d.nets = ["104.16.0.0/24"]
        d.nets_colos = ["HKG"]
        self.store.save()

        from cfopt.store import DomainStore
        store2 = DomainStore(self.store.path, log=lambda m: None)
        got = store2.list()[0]
        self.assertEqual(got.nets, ["104.16.0.0/24"])
        self.assertEqual(got.nets_colos, ["HKG"])


if __name__ == "__main__":
    unittest.main()
