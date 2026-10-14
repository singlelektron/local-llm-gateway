"""Dependency-free, metadata-only HTML for the local usage dashboard."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape
from importlib.resources import files
from urllib.parse import urlencode


_CSS = files("local_llm_gateway").joinpath("dashboard.css").read_text(encoding="utf-8")

_STATUS_LABELS = {
    "success": "成功", "failed": "失败", "cancelled": "已取消", "partial": "部分完成",
    "interrupted": "已中断", "in_progress": "处理中",
}


def _text(value: object, fallback: str = "—") -> str:
    return escape(fallback if value is None else str(value), quote=True)


def _number(value: int | float | None) -> str:
    return "—" if value is None else _text(f"{value:,}")


def _duration(value: int | float | None) -> str:
    if value is None:
        return "—"
    if value < 1000:
        return _text(f"{value:,.0f}") + '<span class="unit">ms</span>'
    return _text(f"{value / 1000:,.2f}") + '<span class="unit">s</span>'


def _bytes(value: int | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            digits = f"{amount:,.0f}" if unit == "B" else f"{amount:,.1f}"
            return _text(digits) + f'<span class="unit">{unit}</span>'
        amount /= 1024
    raise AssertionError("Unreachable byte unit")


def _icon(name: str) -> str:
    paths = {
        "grid": '<rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/>',
        "code": '<path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-14-2 18"/>',
        "server": '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6"/>',
        "refresh": '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 6a8 8 0 0 1 13.2 3M4.7 15A8 8 0 0 0 18 18"/>',
        "arrow": '<path d="M5 12h14m-6-6 6 6-6 6"/>',
        "pulse": '<path d="M2 12h5l3-8 4 16 3-8h5"/>',
    }
    return '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths[name] + '</svg>'


def _select(name: str, label: str, choices: dict[str, list[str]], filters: dict[str, str]) -> str:
    selected = filters.get(name, "")
    values = list(dict.fromkeys(choices.get(name, [])))
    if selected and selected not in values:
        values.append(selected)
    options = ['<option value="">全部</option>']
    for value in sorted(values):
        state = " selected" if value == selected else ""
        options.append(f'<option value="{_text(value)}"{state}>{_text(value)}</option>')
    return f'<label class="filter-field" for="{name}"><span>{label}</span><select id="{name}" name="{name}">' + "".join(options) + '</select></label>'


def _date_value(value: str | None, fallback: date) -> date:
    try:
        return date.fromisoformat(value) if value else fallback
    except ValueError:
        return fallback


def _chart(daily: list[dict], filters: dict[str, str], now: datetime) -> str:
    end = _date_value(filters.get("until"), now.date())
    start = end - timedelta(days=min(13, end.toordinal() - 1))
    start = max(start, _date_value(filters.get("since"), start))
    if start > end:
        return '<div class="empty-state">所选日期范围内暂无可展示日期。</div>'
    dates = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    counts = {row.get("date"): row.get("request_count", 0) for row in daily}
    maximum = max((counts.get(day.isoformat(), 0) for day in dates), default=0)
    bars = []
    for index, day in enumerate(dates):
        count = counts.get(day.isoformat(), 0)
        height = 0 if maximum == 0 else 100 * count / maximum
        label = day.strftime("%m.%d")
        show_label = index in (0, len(dates) - 1) or index % 2 == 0
        bars.append(
            f'<div class="chart-column" title="{day.isoformat()} UTC · {_number(count)} 次请求">'
            f'<span class="bar-count">{_number(count) if count else ""}</span>'
            f'<div class="bar-track"><div class="bar{" bar-zero" if not count else ""}" style="height:{height:.4f}%"></div></div>'
            f'<span class="bar-label{" label-muted" if not show_label else ""}">{label}</span></div>'
        )
    total = sum(counts.get(day.isoformat(), 0) for day in dates)
    return (
        f'<div class="chart-meta"><span>{start.isoformat()} — {end.isoformat()} <span class="muted">UTC</span></span>'
        f'<span><span class="legend-dot"></span>请求数 · {_number(total)}</span></div>'
        '<div class="bar-chart" role="img" aria-label="每日请求量，详细数值见下方可展开的数据表">'
        + "".join(bars) + '</div>'
        '<details class="chart-data"><summary>查看每日数值</summary><div class="table-scroll"><table><caption class="sr-only">每日请求量（UTC）</caption><thead><tr><th scope="col">日期（UTC）</th><th scope="col">请求数</th></tr></thead><tbody>'
        + "".join(f'<tr><th scope="row">{day.isoformat()}</th><td>{_number(counts.get(day.isoformat(), 0))}</td></tr>' for day in dates)
        + '</tbody></table></div></details>'
    )


def _client_usage(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty-state"><span class="empty-mark">↗</span><strong>等待第一次调用</strong><p>客户端经由 Gateway 调用模型后，用量会显示在这里。</p></div>'
    ordered = sorted(rows, key=lambda row: (row.get("total_tokens") or 0, row["request_count"]), reverse=True)
    maximum = max((row.get("total_tokens") or 0 for row in ordered), default=0)
    result = []
    for row in ordered:
        tokens = row.get("total_tokens")
        ratio = 0 if not maximum or tokens is None else 100 * tokens / maximum
        result.append(
            '<div class="client-item"><div class="client-top">'
            f'<span class="client-name">{_text(row.get("client_id"), "未识别客户端")}</span>'
            f'<span class="client-tokens">{_number(tokens)} <span class="muted">Token</span></span></div>'
            f'<div class="usage-track"><span style="width:{ratio:.4f}%"></span></div>'
            f'<div class="client-sub"><span>{_number(row["request_count"])} 次请求</span>'
            f'<span>Token 已知 {_number(row.get("total_token_samples", 0))} / {_number(row["request_count"])}</span></div></div>'
        )
    return '<div class="client-list">' + "".join(result) + '</div>'


def _model_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty-state compact"><strong>还没有模型用量</strong><p>统计会按实际推理模型和后端归类。</p></div>'
    result = []
    for row in sorted(rows, key=lambda item: item["request_count"], reverse=True):
        count = row["request_count"]
        result.append(
            '<tr>'
            f'<th scope="row" class="model-cell">{_text(row.get("model"), "尚未确定")}</th>'
            f'<td><span class="backend-tag">{_text(row.get("backend"), "尚未确定")}</span></td>'
            f'<td class="numeric">{_number(count)}</td>'
            f'<td class="numeric">{_number(row.get("total_tokens"))}<span class="cell-note">已知 {_number(row.get("total_token_samples", 0))} / {_number(count)}</span></td>'
            f'<td class="numeric">{_duration(row.get("avg_duration_ms"))}</td></tr>'
        )
    return '<div class="table-scroll"><table class="models-table"><caption class="sr-only">按模型和后端汇总的用量</caption><thead><tr><th scope="col">实际模型</th><th scope="col">后端</th><th scope="col" class="numeric">请求数</th><th scope="col" class="numeric">已知 Token</th><th scope="col" class="numeric">平均耗时</th></tr></thead><tbody>' + "".join(result) + '</tbody></table></div>'


def _recent_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty-state compact"><strong>没有符合筛选条件的请求</strong><p>尝试调整筛选，或使用独立客户端发起一次调用。</p></div>'
    result = []
    for row in rows[:30]:
        status = row.get("status")
        safe_status = status if status in _STATUS_LABELS else "unknown"
        status_label = _STATUS_LABELS.get(status, "未知")
        error = row.get("error_category")
        timestamp = row.get("started_at")
        try:
            started = datetime.fromisoformat(timestamp).astimezone(timezone.utc)
            time_label, day_label = started.strftime("%H:%M:%S"), started.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            time_label, day_label = "—", "—"
        actual = row.get("actual_model")
        requested = row.get("requested_model")
        model_note = f'<span class="cell-note">别名 {_text(requested)}</span>' if requested and actual != requested else ""
        source = {"measured": "实测", "partial": "部分实测", "unavailable": "未知"}.get(row.get("token_source"), "未知")
        speed = "—" if row.get("generation_tps") is None else _text(f'{row["generation_tps"]:,.1f}')
        result.append(
            f'<tr data-request-id="{_text(row.get("id"))}">'
            f'<td class="timestamp"><time datetime="{_text(timestamp)}">{_text(time_label)}<span class="cell-note">{_text(day_label)}</span></time></td>'
            f'<td class="client-cell">{_text(row.get("client_id"), "未识别")}</td>'
            f'<td class="model-cell">{_text(actual, "尚未确定")}{model_note}<span class="cell-note">{_text(row.get("backend"))} · {"流式" if row.get("stream") else "普通"}</span></td>'
            f'<td><span class="status status-{safe_status}"><span class="status-dot"></span>{status_label}</span>'
            + (f'<span class="cell-note error-category">{_text(error)}</span>' if error else "")
            + '</td>'
            f'<td class="numeric token-cell">{_number(row.get("input_tokens"))} <span class="muted">/</span> {_number(row.get("output_tokens"))}<span class="cell-note">{source}</span></td>'
            f'<td class="numeric">{_duration(row.get("duration_ms"))}</td>'
            f'<td class="numeric">{_duration(row.get("ttft_ms"))}</td>'
            f'<td class="numeric">{speed}</td></tr>'
        )
    return '<div class="table-scroll"><table class="recent-table"><caption class="sr-only">最近的请求及性能，时间为 UTC</caption><thead><tr><th scope="col">时间 · UTC</th><th scope="col">客户端</th><th scope="col">实际模型 / 后端</th><th scope="col">结果</th><th scope="col" class="numeric">输入 / 输出 Token</th><th scope="col" class="numeric">总耗时</th><th scope="col" class="numeric">首 Token</th><th scope="col" class="numeric">Token/s</th></tr></thead><tbody>' + "".join(result) + '</tbody></table></div>'


def render_dashboard(
    *, totals: dict, clients: list[dict], models: list[dict], daily: list[dict],
    recent: list[dict], filters: dict[str, str], choices: dict[str, list[str]],
    active_requests: int, now: datetime, refresh: bool, error: str | None = None,
) -> str:
    """Render only recorded metadata; missing measurements stay visibly unknown."""
    css = _CSS
    now = now.astimezone(timezone.utc)
    count = totals.get("request_count", 0)
    successful = totals.get("success_count", 0)
    pending_note = (
        f'<span class="metric-divider">·</span>处理中 {_number(totals["in_progress_count"])}'
        if totals.get("in_progress_count") else ""
    )
    unsuccessful = sum(totals.get(f"{status}_count", 0) for status in ("failed", "cancelled", "partial", "interrupted"))
    success_rate = "—" if not count else f"{100 * successful / count:.0f}%"
    query = dict(filters)
    if refresh:
        query["refresh"] = "1"
    refresh_url = "/" + ("?" + urlencode(query) if query else "")
    stats_query = {**filters, "group_by": "client_id,model,backend"}
    stats_url = "/admin/stats?" + urlencode(stats_query)
    selected_count = sum(bool(value) for value in filters.values())
    error_html = f'<div class="validation-error" role="alert"><strong>无法应用筛选</strong><span>{_text(error)}</span></div>' if error else ""
    metrics = f'''
      <section class="metrics" aria-label="核心用量统计">
        <article class="metric"><span class="metric-label">总请求数</span><div class="metric-value"><span data-metric="request-count">{_number(count)}</span><span class="unit">次</span></div><div class="metric-foot"><span class="small-dot"></span>成功 {_number(successful)}<span class="metric-divider">·</span>其他结果 {_number(unsuccessful)}{pending_note}</div></article>
        <article class="metric"><span class="metric-label">已知 Token</span><div class="metric-value" data-metric="total-tokens">{_number(totals.get("total_tokens"))}</div><div class="metric-foot">已知 {_number(totals.get("total_token_samples", 0))} / {_number(count)} 次请求<span class="metric-divider">·</span>缺失不计为 0</div></article>
        <article class="metric"><span class="metric-label">平均总耗时</span><div class="metric-value">{_duration(totals.get("avg_duration_ms"))}</div><div class="metric-foot">基于 {_number(totals.get("duration_samples", 0))} 次测量</div></article>
        <article class="metric"><span class="metric-label">响应正文流量</span><div class="metric-value">{_bytes(totals.get("response_bytes"))}</div><div class="metric-foot">请求 {_bytes(totals.get("request_bytes"))}<span class="metric-divider">·</span>响应已知 {_number(totals.get("response_byte_samples", 0))} / {_number(count)}</div><div class="metric-foot minor">请求已知 {_number(totals.get("request_byte_samples", 0))} / {_number(count)}</div></article>
      </section>'''
    form = '<form class="filter-form" action="/" method="get" aria-label="统计筛选">'
    form += _select("client_id", "客户端", choices, filters)
    form += _select("actual_model", "实际模型", choices, filters)
    form += _select("backend", "推理后端", choices, filters)
    for name, label in (("since", "开始日期 · UTC"), ("until", "结束日期 · UTC")):
        form += f'<label class="filter-field date-field" for="{name}"><span>{label}</span><input type="date" id="{name}" name="{name}" value="{_text(filters.get(name, ""))}"></label>'
    form += f'<div class="filter-actions"><button class="button button-primary" type="submit">应用筛选</button><a class="reset-link" href="/">重置</a></div><div class="filter-bottom"><label class="refresh-toggle"><input type="checkbox" name="refresh" value="1"{" checked" if refresh else ""}>每 15 秒自动刷新</label><span>{"已启用 " + str(selected_count) + " 项筛选" if selected_count else "展示全部已记录请求"} · 日期按 UTC 计算</span></div></form>'
    return f'''<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">{"<meta http-equiv='refresh' content='15'>" if refresh else ""}<title>用量概览 · Local Gateway</title><style>{css}</style></head>
<body><a class="skip-link" href="#main">跳转到统计内容</a>
<div class="app-shell">
  <aside class="sidebar" aria-label="主导航">
    <a class="brand" href="/" aria-label="Local Gateway 首页"><span class="brand-symbol">{_icon("pulse")}</span><span>Local Gateway<span class="brand-sub">MODEL INFRASTRUCTURE</span></span></a>
    <div class="nav-caption">WORKSPACE</div><nav><a class="nav-item active" href="/" aria-label="用量概览" aria-current="page">{_icon("grid")}<span>用量概览</span><span class="nav-active-dot"></span></a><a class="nav-item" href="{_text(stats_url)}" aria-label="统计 JSON">{_icon("code")}<span>统计 JSON</span></a><a class="nav-item" href="/admin/backends" aria-label="后端与模型">{_icon("server")}<span>后端与模型</span></a></nav>
    <div class="sidebar-footer"><span class="privacy-symbol">◈</span><strong>Local by design</strong><p>仅记录调用元数据<br>不保存提示词与模型回复</p><span class="version-label">LOCAL LLM GATEWAY</span></div>
  </aside>
  <main id="main" class="main-content">
    <header class="page-header"><div><div class="eyebrow">GATEWAY / ANALYTICS</div><h1>用量概览<span class="heading-dot">.</span></h1><p class="page-description">每一次调用，都有迹可循。</p></div><div class="header-actions"><span class="live-badge"><span class="live-dot"></span>Gateway 运行中</span><a class="button button-secondary" href="{_text(refresh_url)}">{_icon("refresh")}刷新数据</a><span class="last-updated">更新于 {now.strftime("%H:%M:%S")} UTC</span></div></header>
    {error_html}
    <section class="filter-panel" aria-label="筛选条件">{form}</section>
    {metrics}
    <div class="analysis-grid">
      <section class="panel trend-panel"><div class="section-heading"><div><h2>请求趋势</h2><p>所选范围内最后 14 个自然日</p></div><span class="neutral-tag">UTC</span></div>{_chart(daily, filters, now)}</section>
      <section class="panel clients-panel"><div class="section-heading"><div><h2>客户端用量</h2><p>按已知 Token 排序</p></div><span class="count-tag">{_number(len(clients))}</span></div>{_client_usage(clients)}</section>
    </div>
    <section class="panel model-panel"><div class="section-heading"><div><h2>模型分布</h2><p>实际模型与推理后端的用量</p></div><span class="muted section-hint">{_number(len(models))} 个模型 / 后端组合</span></div>{_model_table(models)}</section>
    <section class="panel recent-panel"><div class="section-heading"><div><h2>最近请求</h2><p>性能、用量与请求结果</p></div><span class="muted section-hint">最近 {min(len(recent), 30)} 条</span></div>{_recent_table(recent)}</section>
    <footer class="page-footer"><div><span class="footer-dot"></span>当前处理中 <strong>{_number(active_requests)}</strong><span class="footer-divider">/</span>筛选内成功占比 <strong>{_text(success_rate)}</strong></div><p>— 表示不可用 · Token 来自后端测量 · 字节仅统计应用正文，不含网络协议开销</p></footer>
  </main>
</div></body></html>'''
