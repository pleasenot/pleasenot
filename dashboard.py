#!/usr/bin/env python3
"""
实时交易 Dashboard — 独立脚本，不影响 bot 运行

读取 positions.json / trade_signals.log / api_health.json / bot_daemon_out.log
实时查询 XXYY API 获取当前价格和钱包余额

用法: python dashboard.py
"""
import asyncio
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import config
from xxyy.client import monitor_client as client

BASE_DIR = Path(__file__).parent
POSITIONS_FILE = BASE_DIR / "positions.json"
HEALTH_FILE = BASE_DIR / "api_health.json"
SIGNAL_LOG = BASE_DIR / "trade_signals.log"
BOT_LOG = BASE_DIR / "bot_daemon_out.log"

# trade_signals.log 解析正则
BUY_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] BUY (?P<tier>\S+)\((?P<score>\d+)分\) "
    r"ca=(?P<ca>\S+) amount=(?P<amount>[\d.]+)SOL source=(?P<source>\S+)"
)
SELL_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] SELL \[(?P<reason>[^\]]+)\] (?P<pct>\d+)% "
    r"ca=(?P<ca>\S+)"
)


def load_positions() -> list[dict]:
    try:
        if POSITIONS_FILE.exists():
            return json.loads(POSITIONS_FILE.read_text())
    except Exception:
        pass
    return []


def load_health() -> dict:
    try:
        if HEALTH_FILE.exists():
            return json.loads(HEALTH_FILE.read_text())
    except Exception:
        pass
    return {}


def parse_trade_log() -> dict:
    """解析 trade_signals.log 统计交易数据"""
    stats = {
        "buys": 0, "sells": 0, "total_invested": 0.0,
        "sources": defaultdict(int), "tiers": defaultdict(int),
        "exit_reasons": defaultdict(int), "recent": [],
    }
    if not SIGNAL_LOG.exists():
        return stats
    try:
        for line in SIGNAL_LOG.read_text().splitlines():
            m = BUY_RE.match(line)
            if m:
                stats["buys"] += 1
                stats["total_invested"] += float(m.group("amount"))
                stats["sources"][m.group("source")] += 1
                stats["tiers"][m.group("tier")] += 1
                stats["recent"].append(f"BUY {m.group('tier')} {m.group('ca')[:12]}... {m.group('amount')}SOL")
                continue
            m = SELL_RE.match(line)
            if m:
                stats["sells"] += 1
                stats["exit_reasons"][m.group("reason")] += 1
                stats["recent"].append(f"SELL [{m.group('reason')}] {m.group('ca')[:12]}...")
    except Exception:
        pass
    stats["recent"] = stats["recent"][-10:]  # 最近10条
    return stats


def tail_bot_log(n: int = 15) -> list[str]:
    """读取 bot 日志最后 n 行"""
    try:
        if not BOT_LOG.exists():
            return []
        lines = BOT_LOG.read_text().splitlines()
        return lines[-n:]
    except Exception:
        return []


def check_bot_process() -> str:
    """检查 bot 进程是否运行（兼容 Windows/Linux）"""
    try:
        import subprocess, platform
        if platform.system() == "Windows":
            result = subprocess.run(
                ["wmic", "process", "where", "commandline like '%main.py%--daemon%'", "get", "processid"],
                capture_output=True, text=True,
            )
            pids = [p.strip() for p in result.stdout.split("\n") if p.strip().isdigit()]
        else:
            result = subprocess.run(
                ["pgrep", "-f", "main.py.*--daemon"],
                capture_output=True, text=True,
            )
            pids = [p for p in result.stdout.strip().split("\n") if p]
        if pids:
            return f"[green]运行中[/] PID:{pids[0]}"
        return "[red]未运行[/]"
    except Exception:
        return "[yellow]未知[/]"


def fmt_time_ago(ts: float) -> str:
    if ts <= 0:
        return "N/A"
    delta = time.time() - ts
    if delta < 60:
        return f"{delta:.0f}s"
    if delta < 3600:
        return f"{delta/60:.0f}m"
    return f"{delta/3600:.1f}h"


def fmt_usd(v: float) -> str:
    if v >= 1:
        return f"${v:,.2f}"
    if v >= 0.001:
        return f"${v:.4f}"
    return f"${v:.8f}"


class Dashboard:
    def __init__(self):
        self.console = Console()
        self.sol_balance = 0.0
        self.prices: dict[str, float] = {}
        self.sol_usd = 0.0

    async def fetch_live_data(self, positions: list[dict]):
        """异步获取实时数据：钱包余额 + 持仓价格"""
        try:
            info = await client.wallet_info(config.wallet_address, config.default_chain)
            self.sol_balance = float((info or {}).get("balance", 0) or 0)
        except Exception:
            pass

        # 获取 SOL 价格
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0, verify=False) as http:
                resp = await http.get(
                    "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"
                )
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs") or []
                    if pairs:
                        self.sol_usd = float(pairs[0].get("priceUsd", 0) or 0)
        except Exception:
            pass

        # 获取每个持仓的当前价格
        for pos in positions:
            ca = pos.get("token_address", "")
            if not ca:
                continue
            try:
                data = await client.query_token(ca, pos.get("chain", "sol"))
                if isinstance(data, dict):
                    ti = data.get("tradeInfo") or {}
                    self.prices[ca] = float(ti.get("price", 0) or 0)
            except Exception:
                pass
            await asyncio.sleep(1)  # 避免 429

    def build_header(self) -> Panel:
        bot_status = check_bot_process()
        health = load_health()
        health_str = "[green]健康[/]" if health.get("healthy", True) else f"[red]异常 连续失败:{health.get('consecutive_failures', 0)}[/]"
        api_stats = f"成功:{health.get('total_success', 0)} 失败:{health.get('total_failures', 0)}"

        sol_usd_str = f"(${self.sol_usd:,.0f}/SOL)" if self.sol_usd > 0 else ""
        balance_usd = f" ≈ ${self.sol_balance * self.sol_usd:,.2f}" if self.sol_usd > 0 else ""

        text = Text()
        text.append("  Bot: ", style="bold")
        text.append_text(Text.from_markup(bot_status))
        text.append(f"  |  钱包: {self.sol_balance:.4f} SOL{balance_usd} {sol_usd_str}")
        text.append(f"  |  API: ")
        text.append_text(Text.from_markup(health_str))
        text.append(f" ({api_stats})")
        text.append(f"  |  {time.strftime('%H:%M:%S')}")

        return Panel(text, title="[bold cyan]Solana Meme Bot Dashboard[/]", border_style="cyan")

    def build_positions(self, positions: list[dict]) -> Panel:
        table = Table(expand=True, show_header=True, header_style="bold magenta")
        table.add_column("代币", width=14)
        table.add_column("入场价", justify="right", width=14)
        table.add_column("现价", justify="right", width=14)
        table.add_column("盈亏", justify="right", width=10)
        table.add_column("仓位", justify="right", width=8)
        table.add_column("持仓时间", justify="right", width=8)
        table.add_column("状态", width=10)

        total_pnl_usd = 0.0
        total_cost_usd = 0.0

        if not positions:
            table.add_row("[dim]暂无持仓[/]", "", "", "", "", "", "")
        else:
            for pos in positions:
                ca = pos.get("token_address", "")
                entry = pos.get("entry_price", 0)
                current = self.prices.get(ca, 0)
                buy_sol = pos.get("buy_amount", 0) or config.buy_amount
                status = pos.get("status", "open")
                tp = pos.get("tp_level", 0)
                trailing = pos.get("trailing_active", False)
                entry_time = pos.get("entry_time", 0)

                # 盈亏
                if entry > 0 and current > 0:
                    multi = current / entry
                    pnl_pct = (multi - 1) * 100
                    pnl_usd = buy_sol * self.sol_usd * (multi - 1)
                    total_pnl_usd += pnl_usd
                    total_cost_usd += buy_sol * self.sol_usd

                    if pnl_pct >= 100:
                        pnl_str = f"[bold green]+{pnl_pct:.0f}%[/]"
                    elif pnl_pct >= 0:
                        pnl_str = f"[green]+{pnl_pct:.1f}%[/]"
                    elif pnl_pct > -30:
                        pnl_str = f"[yellow]{pnl_pct:.1f}%[/]"
                    else:
                        pnl_str = f"[red]{pnl_pct:.1f}%[/]"
                else:
                    pnl_str = "[dim]...[/]"

                # 状态
                if trailing:
                    status_str = f"[cyan]移动止盈 TP{tp}[/]"
                elif tp > 0:
                    status_str = f"[green]TP{tp}[/]"
                else:
                    status_str = "[dim]监控中[/]"

                table.add_row(
                    ca[:12] + "...",
                    fmt_usd(entry) if entry > 0 else "待定",
                    fmt_usd(current) if current > 0 else "查询中",
                    pnl_str,
                    f"{buy_sol:.3f}",
                    fmt_time_ago(entry_time),
                    status_str,
                )

        # 汇总行
        if total_cost_usd > 0:
            roi = total_pnl_usd / total_cost_usd * 100
            emoji = "+" if total_pnl_usd >= 0 else ""
            color = "green" if total_pnl_usd >= 0 else "red"
            table.add_row(
                f"[bold]合计 ({len(positions)})[/]", "", "",
                f"[bold {color}]{emoji}${total_pnl_usd:.2f} ({emoji}{roi:.1f}%)[/]",
                "", "", "",
            )

        return Panel(table, title=f"[bold]持仓 ({len(positions)})[/]", border_style="green")

    def build_trade_stats(self, stats: dict) -> Panel:
        lines = []
        lines.append(f"买入: [green]{stats['buys']}[/] 笔  |  卖出: [yellow]{stats['sells']}[/] 笔  |  投入: {stats['total_invested']:.3f} SOL")

        # 信号源
        if stats["sources"]:
            src_str = "  ".join(f"{s}:{c}" for s, c in sorted(stats["sources"].items(), key=lambda x: -x[1])[:5])
            lines.append(f"信号源: {src_str}")

        # 评分档位
        if stats["tiers"]:
            tier_str = "  ".join(f"{t}:{c}" for t, c in stats["tiers"].items())
            lines.append(f"评分档: {tier_str}")

        # 退出原因
        if stats["exit_reasons"]:
            exit_str = "  ".join(f"{r}:{c}" for r, c in sorted(stats["exit_reasons"].items(), key=lambda x: -x[1])[:5])
            lines.append(f"退出: {exit_str}")

        return Panel("\n".join(lines), title="[bold]交易统计[/]", border_style="yellow")

    def build_recent(self, stats: dict) -> Panel:
        if stats["recent"]:
            text = "\n".join(stats["recent"][-8:])
        else:
            text = "[dim]暂无交易记录[/]"
        return Panel(text, title="[bold]最近交易[/]", border_style="blue")

    def build_log(self) -> Panel:
        lines = tail_bot_log(12)
        # 简化日志显示
        short = []
        for line in lines:
            # 去掉时间戳前缀里的日期部分，只保留时间
            line = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", line)
            # 高亮关键词
            if "PASS" in line:
                line = f"[green]{line}[/]"
            elif "REJECT" in line or "fatal" in line:
                line = f"[red]{line}[/]"
            elif "swap" in line.lower() and ("成功" in line or "submitted" in line):
                line = f"[bold green]{line}[/]"
            elif "ERROR" in line or "失败" in line:
                line = f"[yellow]{line}[/]"
            elif "信号触发" in line:
                line = f"[cyan]{line}[/]"
            short.append(line)
        text = "\n".join(short) if short else "[dim]等待日志...[/]"
        return Panel(Text.from_markup(text), title="[bold]实时日志[/]", border_style="white")

    def build_layout(self, positions, stats) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="log", size=16),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=3),
            Layout(name="right", ratio=2),
        )
        layout["right"].split_column(
            Layout(name="stats"),
            Layout(name="recent"),
        )

        layout["header"].update(self.build_header())
        layout["left"].update(self.build_positions(positions))
        layout["stats"].update(self.build_trade_stats(stats))
        layout["recent"].update(self.build_recent(stats))
        layout["log"].update(self.build_log())

        return layout

    async def run(self):
        self.console.clear()
        self.console.print("[bold cyan]Starting Dashboard...[/] (Ctrl+C 退出)\n")

        with Live(console=self.console, refresh_per_second=1, screen=True) as live:
            cycle = 0
            while True:
                positions = load_positions()
                stats = parse_trade_log()

                # 每 10 秒刷新一次实时数据（价格、余额）
                if cycle % 10 == 0:
                    try:
                        await self.fetch_live_data(positions)
                    except Exception:
                        pass

                layout = self.build_layout(positions, stats)
                live.update(layout)

                cycle += 1
                await asyncio.sleep(1)


def main():
    dashboard = Dashboard()
    try:
        asyncio.run(dashboard.run())
    except KeyboardInterrupt:
        print("\nDashboard 已退出")


if __name__ == "__main__":
    main()
