# -*- coding: utf-8 -*-
"""多域名扫描调度。

每个域名独立线程、独立周期、独立网段缓存。
用"到期检查"而不是"每域名一个 sleep"，这样增删域名不需要重启。
"""
import threading
import time
import traceback

from .probe import CF_V4, expand_ranges, nets_from_results, scan


class Scanner:
    def __init__(self, store, workers=200, timeout=3.0, log=print):
        self.store = store
        self.workers = workers
        self.timeout = timeout
        self.log = log
        self._stop = threading.Event()
        self._running = {}          # domain_id -> bool，防止同一域名并发扫
        self._lock = threading.Lock()

    # ---------------------------------------------------------- 单域名扫描
    def scan_one(self, domain):
        """扫描一个域名。失败不影响其他域名。"""
        with self._lock:
            if self._running.get(domain.id):
                self.log(f"[{domain.host}] 已有扫描在进行，跳过")
                return
            self._running[domain.id] = True
        try:
            self._do_scan(domain)
        except Exception:
            err = traceback.format_exc(limit=2)
            domain.mark_failed(err)
            self.log(f"[{domain.host}] 扫描异常：{err}")
        finally:
            with self._lock:
                self._running.pop(domain.id, None)
            self.store.save()

    def _do_scan(self, domain):
        host = domain.host
        self.log(f"[{host}] 开始扫描，目标机房 {','.join(domain.colos)}")

        # 有网段缓存就只扫这些，快很多
        cidrs = domain.nets if domain.nets else CF_V4
        fast = bool(domain.nets)
        ips = expand_ranges(cidrs, domain.per_net)
        self.log(f"[{host}] {'快速' if fast else '全量'}扫描，"
                 f"{len(ips)} 个候选 IP")

        t0 = time.time()
        # 实时进度写进 domain，前端轮询就能看到百分比，
        # 不用干等十几分钟不知道卡在哪
        domain.set_progress(0, len(ips), 0)

        def on_progress(done, total, alive):
            domain.set_progress(done, total, alive)

        results = scan(ips, host, workers=self.workers,
                       timeout=self.timeout, progress=on_progress,
                       refine_top=20)
        domain.clear_progress()
        self.log(f"[{host}] 存活 {len(results)} 个，耗时 {time.time() - t0:.0f}s")

        if not results:
            domain.mark_failed("所有 IP 均不可达")
            return

        dist = {}
        for r in results:
            dist[r["colo"]] = dist.get(r["colo"], 0) + 1

        picked = [r for r in results if r["colo"] in domain.colos]
        if not picked:
            domain.mark_failed(
                f"无 {','.join(domain.colos)} 节点，实际分布 "
                f"{','.join(list(dist.keys())[:5])}")
            return

        picked.sort(key=lambda r: (r["connect_ms"], r["ttfb_ms"]))
        top = picked[: domain.top_n]
        new_ips = [r["ip"] for r in top]

        # 完整候选明细：命中机房的全部，按延迟排序。
        # 界面要靠它显示"扫到了哪些节点、各自延迟多少"，
        # 而不是只看到最终选中的两三个。
        nodes = [{"ip": r["ip"], "colo": r["colo"],
                  "connect_ms": round(r["connect_ms"], 1),
                  "ttfb_ms": round(r["ttfb_ms"], 1)}
                 for r in picked]

        # 首次全量扫描后记录网段，后续走快速模式
        nets = domain.nets
        if not fast:
            primary = domain.colos[0]
            nets = nets_from_results(results, primary, limit=200)
            if not nets:
                nets = nets_from_results(results, picked[0]["colo"], limit=200)

        domain.apply_scan(new_ips, dist, nets, nodes=nodes,
                          total_alive=len(results))
        self.log(f"[{host}] 命中 {len(picked)} 个 {','.join(domain.colos)} 节点，"
                 f"选中 " + ", ".join(
                     f"{r['ip']}({r['connect_ms']:.0f}ms)" for r in top))

    # ---------------------------------------------------------- 调度循环
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        self.log("扫描调度器已启动")

    def _loop(self):
        while not self._stop.is_set():
            now = int(time.time())
            for d in self.store.enabled():
                if now - d.last_scan_at >= d.interval:
                    # 每个域名独立线程，互不阻塞
                    threading.Thread(target=self.scan_one, args=(d,),
                                     daemon=True).start()
                    # 错开一点，避免瞬间打出几百个并发
                    time.sleep(0.5)
            self._stop.wait(30)

    def trigger(self, domain):
        """手动触发，立即返回。"""
        threading.Thread(target=self.scan_one, args=(domain,),
                         daemon=True).start()

    def stop(self):
        self._stop.set()

    def is_running(self, domain_id):
        with self._lock:
            return bool(self._running.get(domain_id))
