// Convert domain-event test fixtures into the AG-UI wire contract. Production never uses this adapter.
export function agUiFrames(run, rows = [], { calls = [], reasoning = '', answer = '' } = {}) {
  const events = [];
  const emit = event => events.push({ sequence: events.length + 1, event });
  const threadId = run.session_id || run.id;
  const messageId = `${run.id}:answer`, reasoningId = `${run.id}:reasoning`;
  let textOpen = false, reasoningOpen = false, finished = false;
  emit({ type: 'RUN_STARTED', threadId, runId: run.id, protocolVersion: '1.0' });
  const close = () => {
    if (reasoningOpen) { emit({ type: 'REASONING_MESSAGE_END', messageId: reasoningId }); emit({ type: 'REASONING_END', messageId: reasoningId }); reasoningOpen = false; }
    if (textOpen) { emit({ type: 'TEXT_MESSAGE_END', messageId }); textOpen = false; }
  };
  const text = delta => {
    if (!textOpen) { emit({ type: 'TEXT_MESSAGE_START', messageId, role: 'assistant' }); textOpen = true; }
    emit({ type: 'TEXT_MESSAGE_CONTENT', messageId, delta });
  };
  const thought = delta => {
    if (!reasoningOpen) { emit({ type: 'REASONING_START', messageId: reasoningId }); emit({ type: 'REASONING_MESSAGE_START', messageId: reasoningId, role: 'reasoning' }); reasoningOpen = true; }
    emit({ type: 'REASONING_MESSAGE_CONTENT', messageId: reasoningId, delta });
  };
  const callActivity = call => emit({ type: 'ACTIVITY_SNAPSHOT', messageId: `${run.id}:activity:${call.call_id}`, activityType: 'y-point.call', content: {
    version: 1, toolCallId: call.call_id, name: call.name, kind: call.kind, status: call.status,
    startedAt: Date.parse(call.started_at || run.create_time) || Date.now(),
    ...(call.elapsed_ms !== null && call.elapsed_ms !== undefined ? { finishedAt: (Date.parse(call.started_at || run.create_time) || Date.now()) + call.elapsed_ms } : {}),
    failureCode: call.failure_code, failureReason: call.failure_reason,
  } });
  const finish = state => {
    close();
    if (state === 'failed' || state === 'timed_out') emit({ type: 'RUN_ERROR', message: '本轮未完成', code: run.failure_code || state });
    else {
      if (state === 'completed') emit({ type: 'CUSTOM', name: 'y-point.answer', value: { version: 1, messageId } });
      emit({ type: 'RUN_FINISHED', threadId, runId: run.id, outcome: { type: state === 'cancelled' ? 'cancelled' : 'success' } });
    }
    finished = true;
  };
  if (reasoning) { thought(reasoning); close(); }
  calls.forEach(callActivity);
  if (answer) text(answer);
  for (const row of rows) {
    if (row.event_type === 'reasoning_delta') thought(row.content);
    else if (row.event_type === 'text_delta') text(row.content);
    else if (row.event_type.startsWith('tool_')) {
      close();
      const known = calls.find(call => call.call_id === row.tool_call_id);
      callActivity({ ...(known || { kind: 'tool', name: row.tool_name }), call_id: row.tool_call_id,
        status: row.event_type === 'tool_started' ? 'running' : row.event_type === 'tool_failed' ? 'failed' : 'succeeded', failure_code: row.failure_code || known?.failure_code });
    } else if (row.event_type === 'state') {
      if (['completed', 'failed', 'cancelled', 'timed_out'].includes(row.state)) finish(row.state);
      else emit({ type: 'ACTIVITY_SNAPSHOT', messageId: `${run.id}:state`, activityType: 'y-point.run', content: { version: 1, state: row.state } });
    }
  }
  if (!finished && ['completed', 'failed', 'cancelled', 'timed_out'].includes(run.state)) finish(run.state);
  return events;
}
export const agUiSse = (runId, frames, after = 0) => frames.filter(frame => frame.sequence > after)
  .map(frame => `id: ${runId}:${frame.sequence}\ndata: ${JSON.stringify(frame.event)}\n\n`).join('');
export const agUiSnapshot = (run, frames, after = 0) => ({
  run, events: frames.filter(frame => frame.sequence > after), latest_sequence: frames.length,
  next_after: Math.max(after, frames.length), through: frames.length, has_more: false,
});

/** Public protocol history plus business metadata; there is no second message history endpoint. */
export function agUiData(path, data) {
  if (!path.includes('/ag-ui/threads/') || !Array.isArray(data)) return data;
  return {
    threadId: path.split('/').at(-1), version: 1,
    messages: data.map(item => ({ id: String(item.id), role: item.role, content: item.content })),
    messageDetails: Object.fromEntries(data.map(({ content, role, ...details }) => [String(details.id), details])),
  };
}
export function agUiPost(route, run, frames = agUiFrames(run, []), after = 0) {
  return route.fulfill({ status: 200, contentType: 'text/event-stream',
    headers: { 'X-Y-Point-Run-ID': run.id, 'X-Y-Point-After': String(after) },
    body: agUiSse(run.id, frames, after),
  });
}
