"""Codex 页面重试和侧栏巡检脚本；每轮只执行一个动作。"""

RETRY_SCRIPT = r"""
(() => {
    const key = '__codexSessionManagerRetryState';
    if (window[key]?.version !== 4) {
        window[key] = {version: 4, retried: new Map(), checked: new Map(), pending: null};
    }
    const state = window[key];
    const now = Date.now();
    const rows = () => [...document.querySelectorAll('[data-app-action-sidebar-thread-row]')];
    const id = row => row?.getAttribute('data-app-action-sidebar-thread-id');
    const selected = () => rows().find(row => row.getAttribute('data-app-action-sidebar-thread-selected') === 'true');
    const currentId = id(selected());
    const visible = element => element && element.getClientRects().length > 0 &&
        !element.closest('[hidden],[aria-hidden="true"],[inert]') &&
        getComputedStyle(element).visibility !== 'hidden';
    // 两类错误都包含容量不足的原因，不能仅凭“压缩任务失败”就重试。
    const capacityPattern = /selected\s+model\s+is\s+at\s+capacity|模型[^。\n]{0,32}(?:容量|满负荷|已满|繁忙|过载|饱和)|容量[^。\n]{0,32}模型/i;
    const actionPattern = /重试|重置|重新尝试|再试一次|retry|try\s+again|reset/i;
    const errorCard = button => {
        // Codex 的错误提示使用 aside；不向上扩大到整个会话，避免误点其他重置按钮。
        const card = button.closest('aside,[role="alert"]');
        return card && visible(card) && capacityPattern.test(card.innerText || '') ? card : null;
    };
    const buttonText = button => [button.innerText || button.textContent || '', button.getAttribute('aria-label') || ''].join(' ')
        .replace(/\s+/g, ' ').trim();
    const countdown = text => [...text.matchAll(/(\d+(?:\.\d+)?)\s*(?:秒|s(?:ec(?:ond)?s?)?\b)/gi)]
        .some(match => Number(match[1]) > 0);
    const lastRetry = threadId => Math.max(state.retried.get(threadId) ?? -Infinity,
        window.__codexSessionManagerRetryCooldowns?.[threadId] ?? -Infinity);
    for (const cache of [state.checked, state.retried]) {
        for (const [threadId, timestamp] of cache) {
            if (now - timestamp > 3600000) cache.delete(threadId);
        }
    }
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
        if (state.pending.visited) {
            state.pending = null;
            return {action: 'cancelled', reason: 'user_navigation'};
        }
        if (now - state.pending.openedAt > 15000) {
            state.pending = null;
            return {action: 'cancelled', reason: 'navigation_timeout'};
        }
        return {action: 'waiting'};
    }
    if (state.pending && currentId === state.pending.targetId) state.pending.visited = true;
    if (state.pending?.clicked) return restore('retry_sent');
    const cooldownKey = currentId || location.href;
    const recent = lastRetry(cooldownKey);
    let candidates = 0;
    for (const button of document.querySelectorAll('button,[role="button"]')) {
        if (button.closest('[data-app-action-sidebar-thread-row]') || !visible(button)) continue;
        const text = buttonText(button);
        if (!actionPattern.test(text)) continue;
        const card = errorCard(button);
        if (!card) continue;
        candidates++;
        if (button.matches(':disabled') || button.closest('[aria-disabled="true"]') ||
            countdown(text) || now - recent < 30000) continue;
        button.click();
        state.retried.set(cooldownKey, now);
        if (state.pending) state.pending.clicked = true;
        return {action: 'retried', threadId: currentId, text, cooldownKey, clickedAt: now};
    }
    if (state.pending) {
        if (now - state.pending.openedAt >= 15000) return restore('no_retry_button');
        return {action: 'waiting', candidates};
    }
    // 用户在输入时不切换会话，避免中断草稿编辑。
    const focused = document.activeElement;
    if (focused?.closest('textarea,input,[contenteditable]:not([contenteditable="false"])')) {
        return {action: 'idle', candidates, reason: 'editing'};
    }
    if (!currentId || !visible(selected())) return {action: 'idle', candidates};
    const target = rows().find(row => {
        const threadId = id(row);
        return threadId && threadId !== currentId && visible(row) &&
            now - (state.checked.get(threadId) ?? -Infinity) >= 30000 &&
            now - lastRetry(threadId) >= 30000 &&
            row.querySelector('svg.text-danger[aria-hidden="false"]');
    });
    if (target) {
        const threadId = id(target);
        state.checked.set(threadId, now);
        state.pending = {targetId: threadId, returnId: currentId, openedAt: now, clicked: false};
        target.click();
        return {action: 'opened', threadId};
    }
    return {action: 'idle', candidates};
})()
"""
