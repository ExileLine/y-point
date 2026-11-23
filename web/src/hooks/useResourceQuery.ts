// -*- coding: utf-8 -*-
// @Time    : 2026-11-23
// @Author  : Codex
// @File    : useResourceQuery.ts

import { useCallback, useEffect, useRef, useState } from "react";

/** 保留上次成功结果，隔离关闭页面及重叠请求的迟到响应。 */
export function useResourceQuery<T>(load: () => Promise<T>, initial: T, active = true) {
  const [data, setData] = useState(initial);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(active);
  const [loadedAt, setLoadedAt] = useState<string>();
  const sequence = useRef(0);
  const live = useRef(false);
  const refresh = useCallback(async () => {
    if (!active || !live.current) return;
    const request = ++sequence.current;
    setLoading(true); setError("");
    try {
      const result = await load();
      if (live.current && request === sequence.current) {
        setData(result); setLoadedAt(new Date().toISOString());
      }
    } catch (failure) {
      if (live.current && request === sequence.current) setError(failure instanceof Error ? failure.message : "查询失败");
    } finally {
      if (live.current && request === sequence.current) setLoading(false);
    }
  }, [active, load]);
  useEffect(() => {
    live.current = true;
    if (active) void refresh();
    else setLoading(false);
    return () => { live.current = false; sequence.current += 1; };
  }, [active, refresh]);
  return { data, error, loading: active && (loading || (!loadedAt && !error)), loadedAt, refresh };
}
