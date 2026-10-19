"""Codex 页面重试和侧栏巡检脚本；每轮只执行一个动作。"""

RETRY_SCRIPT = r"""
(() => {
    const key = '__codexSessionManagerRetryState';
    if (window[key]?.version !== 3) {
        window[key] = {version: 3, clicked: new WeakMap(), checked: new Map(), pending: null};
    }
    const state = window[key];
    const now = Date.now();
    const rows = () => [...document.querySelectorAll('[data-app-action-sidebar-thread-row]')];
    const id = row => row?.getAttribute('data-app-action-sidebar-thread-id');
    const selected = () => rows().find(row => row.getAttribute('data-app-action-sidebar-thread-selected') === 'true');
    const currentId = id(selected());
    const visible = element => element.getClientRects().length > 0 && !element.closest('[hidden],[aria-hidden="true"]');
    const retryPattern = /^(?:retry(?:\s+in\s+\d+\s*(?:s|secs?|seconds?))?|try\s+again|重试|重新尝试|\d+\s*秒\s*后\s*重试)$/i;
    const restore = reason => {
        const pending = state.pending;
        if (!pending.clicked) state.checked.set(pending.targetId, now);
        state.pending = null;
        const row = rows().find(row => id(row) === pending.returnId && visible(row));
        if (row && currentId === pending.targetId) row.click();
        return {action: 'returned', threadId: pending.targetId, reason};
    };
    if (state.pending && currentId !== state.pending.targetId && currentId !== state.pending.returnId) {
        state.pending = null;
        return {action: 'cancelled', reason: 'user_navigation'};
    }
    if (state.pending && currentId === state.pending.returnId) {
        if (now - state.pending.openedAt > 15000) {
            state.pending = null;
            return {action: 'cancelled', reason: 'navigation_timeout'};
        }
        return {action: 'waiting'};
    }
    if (state.pending?.clicked) return restore('retry_sent');
    const cooldownKey = currentId || location.href;
    const recent = state.checked.get(cooldownKey) || 0;
    let candidates = 0;
    for (const button of document.querySelectorAll('button,[role="button"]')) {
        if (button.closest('[data-app-action-sidebar-thread-row]') || !visible(button)) continue;
        const text = (button.textContent || button.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
        if (!retryPattern.test(text)) continue;
        candidates++;
        if (button.disabled || button.getAttribute('aria-disabled') === 'true' ||
            now - recent < 30000 || now - (state.clicked.get(button) || 0) < 30000) continue;
        button.click();
        state.clicked.set(button, now);
        state.checked.set(cooldownKey, now);
        if (state.pending) state.pending.clicked = true;
        return {action: 'retried', threadId: currentId, text};
    }
    if (state.pending) {
        if (now - state.pending.openedAt >= 15000) return restore('no_retry_button');
        return {action: 'waiting', candidates};
    }
    // 用户在输入时不切换会话，避免中断草稿编辑。
    const focused = document.activeElement;
    if (focused?.matches('textarea,input,[contenteditable="true"]')) {
        return {action: 'idle', candidates, reason: 'editing'};
    }
    if (!currentId || !visible(selected())) return {action: 'idle', candidates};
    const target = rows().find(row => {
        const threadId = id(row);
        return threadId && threadId !== currentId && visible(row) &&
            now - (state.checked.get(threadId) || 0) >= 60000 &&
            row.querySelector('svg.text-danger[aria-hidden="false"]');
    });
    if (target) {
        const threadId = id(target);
        state.checked.set(threadId, now - 30000);
        state.pending = {targetId: threadId, returnId: currentId, openedAt: now, clicked: false};
        target.click();
        return {action: 'opened', threadId};
    }
    // 清理长期不再出现的会话记录。
    for (const [threadId, timestamp] of state.checked) {
        if (now - timestamp > 3600000) state.checked.delete(threadId);
    }
    return {action: 'idle', candidates};
})()
"""
